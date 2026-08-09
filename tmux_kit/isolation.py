"""A genuinely isolated, throwaway tmux server -- for tests, examples, and
any agent that needs to poke at real tmux behavior without any chance of
touching the ambient server.

Why this module exists (0.2.1): an agent probing ``remain-on-exit`` behavior
set ``TMUX_TMPDIR`` to a fresh directory and believed that isolated it. It
did not -- the probe was itself running inside a tmux pane, so ``$TMUX`` was
set in its shell, and tmux's socket resolution prefers an inherited ``$TMUX``
over ``TMUX_TMPDIR`` whenever no explicit ``-L``/``-S`` is given. The
resulting `tmux list-sessions` printed the operator's 73 REAL sessions, and a
follow-up `tmux kill-server` destroyed all of them.

**The actual mechanism** (verified against tmux 3.4 on this host):

- ``TMUX_TMPDIR`` alone, with ``$TMUX`` still set and no ``-L``/``-S``: tmux
  uses ``$TMUX`` (the ambient, attached server) and silently ignores
  ``TMUX_TMPDIR``. This is what destroyed 73 sessions.
- An explicit ``-L <name>`` (or ``-S <path>``) DOES override ``$TMUX`` on its
  own -- tmux's socket-path resolution is ``-S`` > ``-L`` > ``$TMUX`` >
  ``TMUX_TMPDIR`` > compiled-in default. Verified: ``tmux -L <random-name>
  list-sessions`` from inside an attached pane correctly errors ``no such
  file or directory`` instead of finding the ambient server.
- So the minimal fix is "always pass ``-L`` (or ``-S``) explicitly, never
  rely on ``TMUX_TMPDIR``/env vars alone." This module goes one step further
  for defense-in-depth: it ALSO scrubs ``$TMUX`` from the child's environment
  and points ``TMUX_TMPDIR`` at a private, freshly-created directory, so
  isolation does not depend on a single correctly-remembered flag either.
  **This defense-in-depth layer is the one that must stay short** (see
  0.2.2 below) -- it is extra insurance on top of the ``-L`` guarantee,
  never a substitute for it.

Why this module changed again (0.2.2): the private ``TMUX_TMPDIR`` directory
above was created under ``tempfile.gettempdir()``, which on macOS resolves to
a long, per-user, per-boot path like
``/var/folders/df/<random>/T/tmux-kit-isolated-dir-<random>``. Once tmux
appends its own ``tmux-<uid>/<socket-name>`` suffix, the full socket path
blew past the 104-byte ``sun_path`` capacity AF_UNIX sockets are limited to
on macOS (108 on Linux, where ``/tmp`` is short enough that this never
surfaced) -- every real tmux invocation failed with ``error connecting to
...: File name too long``. The defensive private-tmpdir layer -- never the
``-L`` guarantee itself -- was what broke. Fixed by anchoring that private
directory under ``/tmp`` (short and present on every platform tmux runs on)
instead of the platform temp dir, and by validating the resulting path
against a safety-margined bound *before* ever invoking tmux, so a future
regression fails loud with an actionable message instead of a cryptic
kernel errno three layers down. See :func:`_short_tmp_base` and
:data:`_MAX_SOCKET_PATH_BYTES`.

This is a stdlib-only, core module (see ``tests/test_rails.py``'s
import-purity rail) -- it is meant to be the obvious thing any consumer,
test, or agent reaches for instead of hand-rolling socket isolation:

    from tmux_kit.isolation import isolated_tmux_server

    async with isolated_tmux_server() as server:
        await server.run("new-session", "-d", "-s", "probe")
        out = await server.run("list-sessions")
    # server is torn down (kill-server + directory removed) here,
    # even if the `with` body raised.

Deliberately independent of ``tmux_kit.proc.run_tmux()`` / the installed
``set_env_factory()`` factory: an isolated server's guarantee must not
depend on what environment factory some host application happens to have
wired up process-wide. ``IsolatedTmuxServer.run()`` builds its own argv and
its own scrubbed environment on every call.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

#: macOS's ``sockaddr_un.sun_path`` capacity for AF_UNIX sockets, in bytes.
#: Linux's is looser (108), but the tighter, cross-platform bound is what
#: gets enforced here -- unconditionally, regardless of which platform is
#: actually running this code -- so a path that only gets exercised in a
#: Linux dev/test run can never quietly regress into something that
#: explodes the next time it runs on macOS (see 0.2.2 module docstring).
_MACOS_UNIX_SOCKET_PATH_LIMIT = 104

#: Headroom below the raw kernel limit above. We can't hand-verify the
#: exact byte count of every future ``prefix=`` a caller might pass, so
#: this bound is checked with margin to spare rather than shaved to the
#: wire.
_SOCKET_PATH_SAFETY_MARGIN = 8

_MAX_SOCKET_PATH_BYTES = _MACOS_UNIX_SOCKET_PATH_LIMIT - _SOCKET_PATH_SAFETY_MARGIN


def _short_tmp_base() -> str:
    """A short, platform-stable base directory for the private
    ``TMUX_TMPDIR`` this module creates.

    Deliberately NOT ``tempfile.gettempdir()``: on macOS that resolves to a
    long, per-user, per-boot path under ``/var/folders/<random>/T`` (often
    50+ bytes on its own), which is exactly what blew the platform's
    104-byte AF_UNIX ``sun_path`` limit once tmux appended its own
    ``tmux-<uid>/<socket-name>`` suffix (see 0.2.2 module docstring).
    ``/tmp`` is short, POSIX-standard, and present on every platform tmux
    itself runs on -- fall back to ``tempfile.gettempdir()`` only if
    ``/tmp`` genuinely isn't there.
    """
    return "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()


def _tmux_socket_path(socket_dir: str, socket_name: str) -> str:
    """The full socket path tmux itself will construct for ``-L
    socket_name`` with ``TMUX_TMPDIR=socket_dir`` -- ``<dir>/tmux-<uid>/<name>``.

    Used to validate the path length *before* ever invoking tmux, so a
    too-long path fails loud with an actionable message here instead of a
    cryptic ``File name too long`` errno from deep inside a subprocess.
    """
    return os.path.join(socket_dir, f"tmux-{os.getuid()}", socket_name)


def _scrubbed_env(tmux_tmpdir: str) -> dict[str, str]:
    """A copy of this process's environment, safe to hand to a throwaway
    tmux server: ``TMUX_TMPDIR`` pinned to *tmux_tmpdir*, ``TMUX`` removed.

    Removing ``TMUX`` is belt-and-suspenders, not the primary guarantee --
    the primary guarantee is that every call in this module passes an
    explicit ``-L`` (see the module docstring for why that alone already
    wins over an inherited ``$TMUX``). Still scrubbed here so this
    module's isolation does not rely on every caller remembering that
    precedence rule correctly.
    """
    env = dict(os.environ)
    env["TMUX_TMPDIR"] = tmux_tmpdir
    env.pop("TMUX", None)
    return env


@dataclass
class IsolatedTmuxServer:
    """A handle to one throwaway, uniquely-socketed tmux server.

    Not constructed directly -- obtained from :func:`isolated_tmux_server`,
    which owns setup and guaranteed teardown.
    """

    socket_name: str
    socket_dir: str

    async def run(self, *args: str) -> str:
        """Run ``tmux -L <this server's socket> <args>`` and return stdout.

        Always targets this server's own unique socket, in its own scrubbed
        environment -- never the ambient one, regardless of what ``$TMUX``
        or ``TMUX_TMPDIR`` happen to be set to in the calling process.

        Raises:
            RuntimeError: the tmux subprocess exited non-zero; the message
                is tmux's own decoded stderr.
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-L",
            self.socket_name,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_scrubbed_env(self.socket_dir),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr_bytes.decode("utf-8", errors="replace"))
        return stdout_bytes.decode("utf-8", errors="replace")


@asynccontextmanager
async def isolated_tmux_server(
    *, prefix: str = "tmux-kit-isolated"
) -> AsyncIterator[IsolatedTmuxServer]:
    """Async context manager yielding a genuinely isolated
    :class:`IsolatedTmuxServer`, guaranteed torn down on exit -- including
    when the ``with`` body raises.

    Isolation layers (any ONE would suffice against the incident this
    module fixes; all three are applied together so no single mistake
    reopens the hole):

    1. A unique socket name (``{prefix}-{uuid4 hex}``) passed via ``-L`` on
       every call -- this alone already overrides an inherited ``$TMUX``
       (see module docstring). Unique per invocation, so concurrent callers
       (parallel test workers, parallel agents) can never collide on the
       same socket name.
    2. A private, freshly-created ``TMUX_TMPDIR`` (``tempfile.mkdtemp``,
       anchored under :func:`_short_tmp_base` rather than the platform temp
       dir -- see 0.2.2 module docstring), torn down with the server -- the
       socket file itself lives nowhere near the ambient server's
       directory.
    3. ``$TMUX`` scrubbed from the child environment on every call.

    Before yielding, the resulting socket path is checked against
    :data:`_MAX_SOCKET_PATH_BYTES`; a path that would exceed it raises
    ``ValueError`` immediately (naming the offending path and byte count)
    rather than letting tmux fail with an opaque ``File name too long``
    once ``run()`` is actually called.

    Teardown runs a ``kill-server`` against this server's own socket
    (errors from "no server was ever started" are swallowed -- there is
    nothing to clean up) and then removes the private temp directory,
    in a ``finally`` block so both happen even if the caller's code inside
    the ``async with`` block raised.
    """
    socket_name = f"{prefix}-{uuid.uuid4().hex[:12]}"
    socket_dir = tempfile.mkdtemp(prefix=f"{prefix}-dir-", dir=_short_tmp_base())
    expected_path = _tmux_socket_path(socket_dir, socket_name)
    path_bytes = len(expected_path.encode("utf-8"))
    if path_bytes > _MAX_SOCKET_PATH_BYTES:
        shutil.rmtree(socket_dir, ignore_errors=True)
        raise ValueError(
            f"Isolated tmux socket path would be {path_bytes} bytes "
            f"({expected_path!r}), exceeding the {_MAX_SOCKET_PATH_BYTES}-byte "
            "safe bound for AF_UNIX sun_path (macOS caps at "
            f"{_MACOS_UNIX_SOCKET_PATH_LIMIT}, Linux at 108). Pass a shorter "
            "prefix= to isolated_tmux_server()."
        )
    server = IsolatedTmuxServer(socket_name=socket_name, socket_dir=socket_dir)
    try:
        yield server
    finally:
        try:
            await server.run("kill-server")
        except RuntimeError:
            # No server was ever started on this socket (nothing to spawn,
            # or the body never called run()) -- nothing to clean up.
            pass
        shutil.rmtree(socket_dir, ignore_errors=True)
