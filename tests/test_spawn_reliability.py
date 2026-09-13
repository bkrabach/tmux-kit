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
