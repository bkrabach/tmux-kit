"""Spawn subprocess-wiring regression tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from tmux_kit import spawn


def _completed_process() -> MagicMock:
    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"", b""))
    return process


def _nonzero_process() -> MagicMock:
    process = _completed_process()
    process.returncode = 1
    return process


async def test_normal_spawn_detaches_child_and_disconnects_stdin(monkeypatch):
    captured = {}

    async def fake_shell(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _completed_process()

    monkeypatch.setattr(spawn, "should_escape", AsyncMock(return_value=False))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    ok, error = await spawn.spawn_session("safe", "echo {name}")

    assert (ok, error) == (True, None)
    assert captured["stdin"] is asyncio.subprocess.DEVNULL
    assert captured["start_new_session"] is True


async def test_nonzero_created_session_is_verified_in_its_explicit_environment(
    monkeypatch,
):
    """A success decision must query only the exact named session and socket."""
    explicit_env = {"TMUX_TMPDIR": "/explicit-socket"}
    checks = []

    async def fake_shell(_command, **_kwargs):
        return _nonzero_process()

    async def fake_session_exists(name, *, env):
        checks.append((name, env))
        return name == "created"

    monkeypatch.setattr(spawn, "should_escape", AsyncMock(return_value=False))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(spawn, "session_exists_strict", fake_session_exists)

    ok, error = await spawn.spawn_session(
        "created", "echo {name}; exit 1", env=explicit_env
    )

    assert (ok, error) == (True, None)
    assert checks == [("created", explicit_env)]
    assert checks[0][1] is explicit_env


async def test_timeout_closes_transport_before_bounded_wait_and_uses_same_env(monkeypatch):
    """A launcher descendant holding pipes cannot extend timeout cleanup."""
    explicit_env = {"TMUX_TMPDIR": "/explicit-socket"}
    process = MagicMock()
    process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    process.kill = MagicMock()
    process._transport = MagicMock()

    async def wait_after_pipes_close():
        assert process._transport.close.called
        raise asyncio.TimeoutError

    process.wait = AsyncMock(side_effect=wait_after_pipes_close)

    async def fake_shell(_command, **_kwargs):
        return process

    session_exists = AsyncMock(return_value=False)
    monkeypatch.setattr(spawn, "should_escape", AsyncMock(return_value=False))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(spawn, "SPAWN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(spawn, "SPAWN_CLEANUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(spawn, "session_exists_strict", session_exists)

    started = asyncio.get_running_loop().time()
    ok, error = await spawn.spawn_session("missing", "sleep 30", env=explicit_env)
    elapsed = asyncio.get_running_loop().time() - started

    assert ok is False
    assert "without creating session" in (error or "")
    assert elapsed < 0.2
    process.kill.assert_called_once_with()
    process._transport.close.assert_called_once_with()
    session_exists.assert_awaited_once_with("missing", env=explicit_env)
    assert session_exists.await_args.kwargs["env"] is explicit_env


async def test_cgroup_escape_spawn_has_the_same_terminal_safety(monkeypatch):
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _completed_process()

    monkeypatch.setattr(spawn, "should_escape", AsyncMock(return_value=True))
    monkeypatch.setattr(spawn, "wrap_shell_argv", lambda command: ["scope", command])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ok, error = await spawn.spawn_session("safe", "echo {name}")

    assert (ok, error) == (True, None)
    assert captured["argv"] == ("scope", "echo safe")
    assert captured["stdin"] is asyncio.subprocess.DEVNULL
    assert captured["start_new_session"] is True
