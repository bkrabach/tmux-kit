"""Unit tests for tmux_kit.mcp_server -- MCP tools over tmux_kit.api.

Skipped entirely if the `mcp` extra isn't installed. This repo's main CI
job doesn't install it by default; a separate job does (see
.github/workflows/test.yml) so this file's tests actually execute in CI,
not just locally.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from tmux_kit import mcp_server


async def test_every_facade_verb_is_registered_as_a_tool():
    tools = await mcp_server.mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "start",
        "list_sessions",
        "status",
        "read",
        "page",
        "search",
        "wait_for_attention",
        "stop",
        "kill",
        "rename",
        "doctor",
    }
    assert expected <= names


async def test_start_tool_delegates_to_api(monkeypatch):
    async def fake_start(name, command=None, *, cwd=None):
        return (True, None)

    monkeypatch.setattr(mcp_server.api, "start", fake_start)
    result = await mcp_server.start("demo")
    assert result == {"ok": True, "error": None}


async def test_list_sessions_tool_serializes_dataclasses(monkeypatch):
    async def fake_list():
        return [
            mcp_server.api.SessionInfo(
                name="a", running=True, activity=None, created=None, cwd=None
            )
        ]

    monkeypatch.setattr(mcp_server.api, "list_sessions", fake_list)
    result = await mcp_server.list_sessions()
    assert result == [
        {"name": "a", "running": True, "activity": None, "created": None, "cwd": None}
    ]


async def test_doctor_tool_returns_dataclass_as_dict(monkeypatch):
    async def fake_doctor():
        return mcp_server.api.DoctorReport(
            tmux_found=True,
            tmux_version="tmux 3.4",
            cgroup_mode="not-applicable",
            cgroup_escape_ready=False,
            socket_dir="/x",
            socket_dir_writable=True,
            notes=[],
        )

    monkeypatch.setattr(mcp_server.api, "doctor", fake_doctor)
    result = await mcp_server.doctor()
    assert result["tmux_found"] is True
    assert result["socket_dir"] == "/x"


async def test_kill_tool_delegates_and_confirms(monkeypatch):
    async def fake_kill(name):
        return None

    monkeypatch.setattr(mcp_server.api, "kill", fake_kill)
    result = await mcp_server.kill("demo")
    assert "demo" in result
