"""Unit tests for the socket-pinned native buffered paste primitive."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tmux_kit import paste, proc
from tmux_kit.keys import MAX_TEXT_BYTES


@pytest.mark.parametrize(
    ("pane_id", "text", "socket_path", "message"),
    [
        ("%4", "ok", "relative/socket", "absolute"),
        ("%4", "ok", "", "absolute"),
        ("%4", "ok", "/tmp/\x00socket", "NUL"),
        ("session", "ok", "/tmp/socket", "pane_id"),
        ("%4:1", "ok", "/tmp/socket", "pane_id"),
        ("%4", "\x00", "/tmp/socket", "C0"),
        ("%4", "\x1b[200~", "/tmp/socket", "ESC"),
        ("%4", "\x1f", "/tmp/socket", "C0"),
        ("%4", "\x7f", "/tmp/socket", "C0"),
    ],
)
async def test_paste_rejects_invalid_input_before_subprocess(
    pane_id, text, socket_path, message, monkeypatch
):
    forbidden = AsyncMock(side_effect=AssertionError("validation contacted tmux"))
    monkeypatch.setattr(paste.proc, "run_tmux", forbidden)

    with pytest.raises(ValueError, match=message):
        await paste.paste_text(pane_id, text, socket_path=socket_path)
    forbidden.assert_not_awaited()


async def test_paste_rejects_utf8_payload_above_byte_limit_before_subprocess(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("validation contacted tmux"))
    monkeypatch.setattr(paste.proc, "run_tmux", forbidden)

    with pytest.raises(ValueError, match="byte"):
        await paste.paste_text("%4", "é" * MAX_TEXT_BYTES, socket_path="/tmp/socket")
    forbidden.assert_not_awaited()


async def test_paste_passes_exact_utf8_bytes_to_a_private_buffer(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict]] = []

    async def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(paste.proc, "run_tmux", fake_run)
    monkeypatch.setenv("TMUX", "ambient,1,0")
    text = "snowman ☃\r\ntrailing\n"

    await paste.paste_text("%42", text, socket_path="/tmp/kit.sock")

    assert len(calls) == 2
    load_args, load_kwargs = calls[0]
    paste_args, paste_kwargs = calls[1]
    buffer_name = load_args[4]
    assert buffer_name.startswith("tmux-kit-paste-")
    assert load_args == (
        "-S",
        "/tmp/kit.sock",
        "load-buffer",
        "-b",
        buffer_name,
        "-",
    )
    assert load_kwargs["input_bytes"] == text.encode("utf-8")
    assert "TMUX" not in load_kwargs["env"]
    assert paste_args == (
        "-S",
        "/tmp/kit.sock",
        "paste-buffer",
        "-p",
        "-r",
        "-d",
        "-b",
        buffer_name,
        "-t",
        "%42",
    )
    assert "Enter" not in paste_args
    assert paste_kwargs["env"] is not load_kwargs["env"]


async def test_paste_success_uses_tmux_delete_flag_for_private_buffer(monkeypatch):
    run_tmux = AsyncMock(return_value="")
    monkeypatch.setattr(paste.proc, "run_tmux", run_tmux)

    await paste.paste_text("%42", "ok", socket_path="/tmp/kit.sock")

    assert run_tmux.await_args_list[1].args[2:6] == ("paste-buffer", "-p", "-r", "-d")
    assert all("delete-buffer" not in call.args for call in run_tmux.await_args_list)


async def test_paste_deletes_private_buffer_when_delivery_fails(monkeypatch):
    failure = RuntimeError("target pane vanished")
    run_tmux = AsyncMock(side_effect=["", failure, ""])
    monkeypatch.setattr(paste.proc, "run_tmux", run_tmux)

    with pytest.raises(RuntimeError, match="target pane vanished") as caught:
        await paste.paste_text("%42", "ok", socket_path="/tmp/kit.sock")

    assert caught.value is failure
    assert run_tmux.await_args_list[2].args[2:5] == (
        "delete-buffer",
        "-b",
        run_tmux.await_args_list[0].args[4],
    )


async def test_paste_deletes_private_buffer_when_cancelled(monkeypatch):
    run_tmux = AsyncMock(side_effect=["", asyncio.CancelledError(), ""])
    monkeypatch.setattr(paste.proc, "run_tmux", run_tmux)

    with pytest.raises(asyncio.CancelledError):
        await paste.paste_text("%42", "ok", socket_path="/tmp/kit.sock")

    assert run_tmux.await_args_list[2].args[2] == "delete-buffer"


async def test_cancelled_load_cleans_accepted_buffer(monkeypatch):
    buffers = set()
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args[2])
        name = args[args.index("-b") + 1]
        if args[2] == "load-buffer":
            buffers.add(name)
            raise asyncio.CancelledError()
        if args[2] == "delete-buffer":
            buffers.remove(name)
        return ""

    monkeypatch.setattr(paste.proc, "run_tmux", fake_run)
    with pytest.raises(asyncio.CancelledError):
        await paste.paste_text("%42", "private\ntext", socket_path="/tmp/kit.sock")
    assert not buffers
    assert calls == ["load-buffer", "delete-buffer"]


async def test_cancelled_successful_paste_keeps_cancellation(monkeypatch):
    async def fake_run(*args, **kwargs):
        if args[2] == "paste-buffer":
            # The remote -d succeeded before cancellation reached its caller.
            raise asyncio.CancelledError()
        if args[2] == "delete-buffer":
            raise RuntimeError(f"unknown buffer: {args[4]}\n")
        return ""

    monkeypatch.setattr(paste.proc, "run_tmux", fake_run)
    with pytest.raises(asyncio.CancelledError):
        await paste.paste_text("%42", "text", socket_path="/tmp/kit.sock")


@pytest.mark.integration
async def test_cancel_during_load_waits_then_cleans_real_buffer(monkeypatch):
    from tmux_kit import isolated_tmux_server

    original_run = proc.run_tmux
    loaded = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls = []

    async def delayed_load(*args, **kwargs):
        calls.append(args[2])
        result = await original_run(*args, **kwargs)
        if args[2] == "load-buffer":
            loaded.set()
            await release.wait()
            finished.set()
        return result

    async with isolated_tmux_server(prefix="paste-cancel") as server:
        await server.run("new-session", "-d", "-s", "receiver")
        pane_id = (await server.run(
            "display-message", "-p", "-t", "=receiver:", "#{pane_id}"
        )).strip()
        socket_path = (await server.run(
            "display-message", "-p", "#{socket_path}"
        )).strip()
        await server.run("set-buffer", "-b", "keep-me", "unrelated text")
        monkeypatch.setattr(paste.proc, "run_tmux", delayed_load)
        task = asyncio.create_task(
            paste.paste_text(pane_id, "private\ntext", socket_path=socket_path)
        )
        try:
            await asyncio.wait_for(loaded.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert finished.is_set(), "cancelled load was not allowed to finish"
            names = await original_run(
                "-S", socket_path, "list-buffers", "-F", "#{buffer_name}"
            )
            assert names.splitlines() == ["keep-me"]
            assert calls == ["load-buffer", "delete-buffer"]
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_paste_reports_both_delivery_and_cleanup_failures(monkeypatch):
    delivery_error = RuntimeError("target pane vanished")
    cleanup_error = RuntimeError("buffer unavailable")
    run_tmux = AsyncMock(side_effect=["", delivery_error, cleanup_error])
    monkeypatch.setattr(paste.proc, "run_tmux", run_tmux)

    with pytest.raises(
        RuntimeError,
        match=r"target pane vanished; private buffer cleanup failed: buffer unavailable",
    ) as caught:
        await paste.paste_text("%42", "ok", socket_path="/tmp/kit.sock")

    assert caught.value.__cause__ is delivery_error


async def test_run_tmux_writes_input_bytes_to_stdin(monkeypatch):
    observed: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, input_bytes=None):
            observed["input_bytes"] = input_bytes
            return b"ok", b""

    async def fake_exec(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(proc.asyncio, "create_subprocess_exec", fake_exec)

    assert await proc.run_tmux("load-buffer", input_bytes=b"\xff\x00") == "ok"
    assert observed["input_bytes"] == b"\xff\x00"
    assert observed["kwargs"]["stdin"] is asyncio.subprocess.PIPE


async def test_run_tmux_preserves_no_stdin_behavior_when_input_is_omitted(monkeypatch):
    observed: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, *args):
            observed["communicate_args"] = args
            return b"", b""

    async def fake_exec(*args, **kwargs):
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(proc.asyncio, "create_subprocess_exec", fake_exec)

    await proc.run_tmux("list-sessions")
    assert "stdin" not in observed["kwargs"]
    assert observed["communicate_args"] == ()