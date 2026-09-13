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
    """A success decision must not observe a different tmux socket."""
    explicit_env = {"TMUX_TMPDIR": "/explicit-socket"}
    observed_envs = []

    async def fake_shell(_command, **_kwargs):
        return _nonzero_process()

    async def fake_strict_enumerate(*, env):
        observed_envs.append(env)
        return ["created"]

    monkeypatch.setattr(spawn, "should_escape", AsyncMock(return_value=False))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(spawn, "enumerate_sessions_strict", fake_strict_enumerate)

    ok, error = await spawn.spawn_session(
        "created", "echo {name}; exit 1", env=explicit_env
    )

    assert (ok, error) == (True, None)
    assert observed_envs == [explicit_env]


async def test_timeout_cleanup_wait_is_bounded_before_session_verification(monkeypatch):
    """A launcher descendant holding pipes cannot extend the timeout forever."""
    process = MagicMock()
    process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    process.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    process.kill = MagicMock()

    async def fake_shell(_command, **_kwargs):
        return process

    monkeypatch.setattr(spawn, "should_escape", AsyncMock(return_value=False))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(spawn, "SPAWN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(spawn, "SPAWN_CLEANUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        spawn, "enumerate_sessions_strict", AsyncMock(return_value=[])
    )

    started = asyncio.get_running_loop().time()
    ok, error = await spawn.spawn_session("missing", "sleep 30")
    elapsed = asyncio.get_running_loop().time() - started

    assert ok is False
    assert "without creating session" in (error or "")
    assert elapsed < 0.2
    process.kill.assert_called_once_with()

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
