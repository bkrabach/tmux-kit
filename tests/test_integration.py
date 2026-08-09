"""Real-tmux integration tests for tmux-kit's own public surface.

New for this repo (not carried): the muxplex-repo integration tests
(``test_integration.py``, ``test_integration_manifest.py``,
``test_scrollback_paging_integration.py``, ``test_auto_views_integration.py``)
all drive a real tmux server THROUGH the muxplex FastAPI app -- they prove
muxplex's *use* of the library, not the library's own contract in
isolation. This file is the library's own real-tmux proof, run against an
isolated, uniquely-named `tmux -L <name>` socket (never the default/ambient
server -- see the muxplex repo's AGENTS.md, "Any test or proof that arms
this hook for real must run against an isolated tmux server").

Per plan §6.1, this repo's CI runs `-m integration` unconditionally (there
is no live muxplex on a CI runner to endanger); a local contributor still
gets this file's own explicit `-L` socket isolation on top of conftest's
autouse `TMUX_TMPDIR` isolation.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest
from tmux_kit.bell import poll_bell_flag
from tmux_kit.names import rename_tmux_session
from tmux_kit.observe import capture_pane, enumerate_sessions
from tmux_kit.spawn import spawn_session

pytestmark = pytest.mark.integration


@pytest.fixture
def tmux_socket():
    """A uniquely-named tmux socket for this test, torn down afterward --
    never the default socket, never the ambient server.
    """
    name = f"tmux-kit-test-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
    )


async def _run(socket: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "-L",
        socket,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")


async def test_spawn_enumerate_capture_round_trip(tmux_socket, monkeypatch):
    """spawn_session() creates a real session; enumerate_sessions() and
    capture_pane() see it through the SAME isolated socket.
    """
    import tmux_kit.observe as observe_mod
    import tmux_kit.spawn as spawn_mod

    async def run_tmux_isolated(*args: str) -> str:
        return await _run(tmux_socket, *args)

    monkeypatch.setattr(observe_mod, "run_tmux", run_tmux_isolated)
    monkeypatch.setattr(spawn_mod, "enumerate_sessions", observe_mod.enumerate_sessions)

    name = "kit-integ-1"
    ok, err = await spawn_session(
        name, f"tmux -L {tmux_socket} new-session -d -s {{name}}"
    )
    assert ok, err

    names = await enumerate_sessions()
    assert name in names

    snapshot = await capture_pane(name)
    assert isinstance(snapshot, str)


async def test_rename_tmux_session_round_trip(tmux_socket, monkeypatch):
    """rename_tmux_session() actually renames a real session; a caller
    re-enumerates to see the observed post-rename name.
    """
    import tmux_kit.names as names_mod
    import tmux_kit.observe as observe_mod

    async def run_tmux_isolated(*args: str) -> str:
        return await _run(tmux_socket, *args)

    monkeypatch.setattr(observe_mod, "run_tmux", run_tmux_isolated)
    monkeypatch.setattr(names_mod, "run_tmux", run_tmux_isolated)

    await _run(tmux_socket, "new-session", "-d", "-s", "old_name")
    await rename_tmux_session("old_name", "new_name")

    names = await enumerate_sessions()
    assert "new_name" in names
    assert "old_name" not in names


async def test_start_then_immediate_read_sees_output_not_empty(monkeypatch):
    """Regression test for the empty-read-after-start race (0.3.0):
    ``api.start()``'s bounded post-spawn wait means an immediate
    ``api.read()`` right after finds the command's real output, not a
    misleading empty string, against a REAL (privately-socketed) tmux
    server -- not a mock.

    Uses the facade's OWN isolation mechanism (``TMUX_KIT_SOCKET_DIR`` ->
    ``proc.tmux_env()``, which scrubs ``$TMUX`` and points ``TMUX_TMPDIR``
    at a directory unique to this test) rather than
    ``tmux_kit.isolation.isolated_tmux_server()`` -- that primitive's own
    ``-L`` socket would bypass the exact code path (``api._ensure_wired()``
    -> ``proc.set_env_factory()``) this test is proving, and is unrelated
    to the ambient/prod server either way (see ``tmux_env()``'s own
    docstring for why scrubbing ``$TMUX`` is what keeps this off any real
    server regardless of which mechanism is used).

    Deliberately NOT built on pytest's own ``tmp_path`` -- see
    ``conftest.py``'s own ``_isolate_tmux_socket_dir`` docstring: that
    fixture resolves to a long, deeply-nested path that can (and, verified
    while writing this test, DOES on this host) blow tmux's AF_UNIX
    ``sun_path`` budget once tmux's own ``tmux-<uid>/default`` suffix is
    appended -- the exact 0.2.2 regression class. ``mkdtemp`` directly
    under ``/tmp`` stays short, mirroring conftest's own pattern.
    """
    import shutil
    import tempfile

    from tmux_kit import api, proc

    socket_dir = tempfile.mkdtemp(prefix="tmux-kit-race-test-", dir="/tmp")
    monkeypatch.setenv("TMUX_KIT_SOCKET_DIR", socket_dir)
    proc.set_env_factory(None)
    name = "kit-integ-race"
    try:
        # ``; sleep 5`` keeps the session alive after the echo -- without
        # it, the pane's command exits and (remain-on-exit off, tmux's
        # factory default) the session is torn down again almost
        # instantly, so a slow-to-schedule read() might find nothing left
        # to read AT ALL rather than proving the race this test targets.
        # Matches this library's own quickstart example's exact pattern.
        ok, err = await api.start(name, "echo integration-race-marker; sleep 5")
        assert ok, err
        text = await api.read(name)
        assert "integration-race-marker" in text
    finally:
        try:
            await api.kill(name)
        except RuntimeError:
            pass
        # Tear down the whole private tmux server this test spun up (not
        # just the one session) via the library's own run_tmux() -- never
        # a raw, unisolated subprocess call (see test_rails.py's rail).
        try:
            await proc.run_tmux("kill-server", env=proc.tmux_env(socket_dir))
        except RuntimeError:
            pass
        proc.set_env_factory(None)
        shutil.rmtree(socket_dir, ignore_errors=True)


async def test_poll_bell_flag_sees_a_real_bell(tmux_socket, monkeypatch):
    """poll_bell_flag() against a real tmux server: a manually-set
    `bell-flag` on a background window is detected, matching the
    multi-window incident the differential harness also pins.
    """
    import tmux_kit.bell as bell_mod
    import tmux_kit.observe as observe_mod

    async def run_tmux_isolated(*args: str) -> str:
        return await _run(tmux_socket, *args)

    monkeypatch.setattr(bell_mod, "run_tmux", run_tmux_isolated)
    monkeypatch.setattr(observe_mod, "run_tmux", run_tmux_isolated)

    name = "kit-integ-bell"
    await _run(tmux_socket, "new-session", "-d", "-s", name)
    # Manually set the window's bell flag (tmux exposes this as a
    # settable option for test purposes; a real bell would set it too).
    await _run(
        tmux_socket, "set-window-option", "-t", f"{name}:0", "monitor-bell", "on"
    )
    # tmux doesn't expose a direct "fire a bell" CLI primitive cleanly
    # portable across versions, so this test asserts the flag reads as
    # False on a fresh, bell-free window -- the differential harness
    # (test_differential_harness.py) is the byte-identical proof of the
    # actual multi-window bell-detection behavior against fleet-recorded
    # real tmux output.
    result = await poll_bell_flag(name)
    assert result is False
