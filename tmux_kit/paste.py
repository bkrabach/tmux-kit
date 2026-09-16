"""Native buffered paste into one immutable tmux pane.

``paste_text()`` transports text through a UUID-named tmux buffer rather than
simulating key events.  It is intentionally narrow: callers supply the exact
absolute socket path and immutable ``%<digits>`` pane ID, and this module never
discovers a server, chooses a pane, reads configuration, or decides whether an
application accepted the paste.  ``paste-buffer -p`` requests bracketed-paste
framing only when the target application has enabled it; otherwise tmux performs
its normal raw native paste.  No Enter event is generated.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

from tmux_kit import proc
from tmux_kit.keys import MAX_TEXT_BYTES

_PANE_ID_RE = re.compile(r"%[0-9]+\Z")


def _validate_paste(pane_id: str, text: str, socket_path: str) -> bytes:
    """Validate all caller-controlled values before contacting tmux."""
    if not isinstance(socket_path, str) or not socket_path:
        raise ValueError("socket_path must be a non-empty absolute path")
    if "\x00" in socket_path:
        raise ValueError("socket_path must not contain NUL")
    if not os.path.isabs(socket_path):
        raise ValueError("socket_path must be an absolute path")
    if not isinstance(pane_id, str) or not _PANE_ID_RE.fullmatch(pane_id):
        raise ValueError("pane_id must be an immutable tmux pane ID like '%42'")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("text must be UTF-8 encodable") from error
    if len(payload) > MAX_TEXT_BYTES:
        raise ValueError(f"text exceeds the {MAX_TEXT_BYTES}-byte UTF-8 limit")
    for character in text:
        codepoint = ord(character)
        if character == "\x1b":
            raise ValueError("text must not contain ESC")
        if codepoint == 0x7F or (codepoint < 0x20 and character not in "\t\r\n"):
            raise ValueError("text must not contain C0 or DEL controls")
    return payload


def _private_env() -> dict[str, str]:
    """Snapshot a child environment that cannot inherit an ambient server."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    return env


async def _finish_tmux(
    *args: str, env: dict[str, str], input_bytes: bytes | None = None
) -> str:
    """Settle the submitted subprocess before propagating cancellation.

    A cancelled load must not finish later and recreate a buffer after cleanup.
    Shield the whole subprocess operation, including its creation and reaping.
    """
    operation = asyncio.create_task(
        proc.run_tmux(*args, env=env, input_bytes=input_bytes)
    )
    cancelled: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break  # operation.result() below reports the original failure
    if cancelled is not None:
        if not operation.cancelled():
            failure = operation.exception()
            if failure is not None:
                cancelled.add_note(f"The tmux operation also failed: {failure}")
        raise cancelled
    return operation.result()


async def paste_text(pane_id: str, text: str, *, socket_path: str) -> None:
    """Paste *text* into one pane via a private, UUID-named tmux buffer.

    The supplied socket path and ``%<digits>`` pane ID are validated before any
    subprocess call.  The payload is loaded byte-for-byte from stdin, then
    ``paste-buffer -p -r -d`` delivers and deletes its private buffer.  If
    loading or delivery fails, cleanup is attempted for only the named buffer.
    Cancellation waits for the submitted tmux operation to finish before
    cleanup, then propagates. If the server prevents cleanup, that failure is
    reported too; cancellation keeps its original exception with a note.
    """
    payload = _validate_paste(pane_id, text, socket_path)
    buffer_name = f"tmux-kit-paste-{uuid.uuid4().hex}"
    env = _private_env()

    try:
        await _finish_tmux(
            "-S",
            socket_path,
            "load-buffer",
            "-b",
            buffer_name,
            "-",
            env=dict(env),
            input_bytes=payload,
        )
        await _finish_tmux(
            "-S",
            socket_path,
            "paste-buffer",
            "-p",
            "-r",
            "-d",
            "-b",
            buffer_name,
            "-t",
            pane_id,
            env=dict(env),
        )
    except BaseException as paste_error:
        try:
            await _finish_tmux(
                "-S",
                socket_path,
                "delete-buffer",
                "-b",
                buffer_name,
                env=dict(env),
            )
        except Exception as cleanup_error:
            # -d may already have removed this exact buffer before cancellation.
            if str(cleanup_error).strip() != f"unknown buffer: {buffer_name}":
                message = (
                    f"paste-buffer failed: {paste_error}; private buffer cleanup failed: "
                    f"{cleanup_error}"
                )
                if isinstance(paste_error, asyncio.CancelledError):
                    paste_error.add_note(message)
                else:
                    raise RuntimeError(message) from paste_error
        except asyncio.CancelledError:
            # _finish_tmux has settled cleanup even on another cancellation.
            if not isinstance(paste_error, asyncio.CancelledError):
                raise
        raise