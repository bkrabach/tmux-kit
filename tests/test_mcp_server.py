"""Unit tests for tmux_kit.mcp_server -- MCP tools over tmux_kit.api.

Skipped entirely if the `mcp` extra isn't installed. This repo's main CI
job doesn't install it by default; a separate job does (see
.github/workflows/test.yml) so this file's tests actually execute in CI,
not just locally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

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
        "exit_code",
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


async def test_exit_code_tool_delegates_to_api(monkeypatch):
    monkeypatch.setattr(mcp_server.api, "exit_code", AsyncMock(return_value=1))
    assert await mcp_server.exit_code("demo") == 1


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
    monkeypatch.setenv("TMUX_KIT_MCP_KILL_ENABLED", "true")
    monkeypatch.setenv("TMUX_KIT_MCP_KILL_ALLOW", "demo")
    result = await mcp_server.kill("demo")
    assert "demo" in result


async def test_start_tool_returns_ok_false_for_a_mangle_prone_name(monkeypatch):
    """api.start() now raises ValueError for a '.'-containing name; the
    MCP tool converts that into the same {"ok": False, "error": ...} shape
    it already uses for ordinary failures, per its documented contract."""
    result = await mcp_server.start("build.js")
    assert result["ok"] is False
    assert "mangle" in result["error"]


# ---------------------------------------------------------------------------
# stop/kill authorization fence -- deny-by-default, operator-configured via
# environment, never grantable by the calling agent itself.
# ---------------------------------------------------------------------------


async def test_stop_denied_by_default(monkeypatch):
    monkeypatch.delenv("TMUX_KIT_MCP_STOP_ENABLED", raising=False)
    monkeypatch.delenv("TMUX_KIT_MCP_STOP_ALLOW", raising=False)
    stop_mock = AsyncMock()
    monkeypatch.setattr(mcp_server.api, "stop", stop_mock)
    with pytest.raises(PermissionError):
        await mcp_server.stop("demo")
    stop_mock.assert_not_awaited()


async def test_kill_denied_by_default(monkeypatch):
    monkeypatch.delenv("TMUX_KIT_MCP_KILL_ENABLED", raising=False)
    monkeypatch.delenv("TMUX_KIT_MCP_KILL_ALLOW", raising=False)
    kill_mock = AsyncMock()
    monkeypatch.setattr(mcp_server.api, "kill", kill_mock)
    with pytest.raises(PermissionError):
        await mcp_server.kill("demo")
    kill_mock.assert_not_awaited()


async def test_stop_allowed_when_enabled_and_name_matches(monkeypatch):
    monkeypatch.setenv("TMUX_KIT_MCP_STOP_ENABLED", "true")
    monkeypatch.setenv("TMUX_KIT_MCP_STOP_ALLOW", "demo-*")
    stop_mock = AsyncMock()
    monkeypatch.setattr(mcp_server.api, "stop", stop_mock)
    result = await mcp_server.stop("demo-1")
    assert "demo-1" in result
    stop_mock.assert_awaited_once_with("demo-1")


async def test_stop_denied_when_enabled_but_name_does_not_match(monkeypatch):
    monkeypatch.setenv("TMUX_KIT_MCP_STOP_ENABLED", "true")
    monkeypatch.setenv("TMUX_KIT_MCP_STOP_ALLOW", "demo-*")
    stop_mock = AsyncMock()
    monkeypatch.setattr(mcp_server.api, "stop", stop_mock)
    with pytest.raises(PermissionError):
        await mcp_server.stop("production-db")
    stop_mock.assert_not_awaited()


async def test_kill_allowed_when_enabled_and_name_matches(monkeypatch):
    monkeypatch.setenv("TMUX_KIT_MCP_KILL_ENABLED", "true")
    monkeypatch.setenv("TMUX_KIT_MCP_KILL_ALLOW", "demo-*")
    kill_mock = AsyncMock()
    monkeypatch.setattr(mcp_server.api, "kill", kill_mock)
    result = await mcp_server.kill("demo-1")
    assert "demo-1" in result
    kill_mock.assert_awaited_once_with("demo-1")


async def test_stop_and_kill_tiers_are_independently_configured(monkeypatch):
    """Authorizing `stop` for a session must NOT also authorize `kill` for
    it -- the two verbs are gated by separate env-var pairs on purpose
    (different blast radius: recoverable vs unrecoverable)."""
    monkeypatch.setenv("TMUX_KIT_MCP_STOP_ENABLED", "true")
    monkeypatch.setenv("TMUX_KIT_MCP_STOP_ALLOW", "*")
    monkeypatch.delenv("TMUX_KIT_MCP_KILL_ENABLED", raising=False)
    monkeypatch.delenv("TMUX_KIT_MCP_KILL_ALLOW", raising=False)

    stop_mock = AsyncMock()
    kill_mock = AsyncMock()
    monkeypatch.setattr(mcp_server.api, "stop", stop_mock)
    monkeypatch.setattr(mcp_server.api, "kill", kill_mock)

    await mcp_server.stop("anything")
    stop_mock.assert_awaited_once_with("anything")

    with pytest.raises(PermissionError):
        await mcp_server.kill("anything")
    kill_mock.assert_not_awaited()


async def test_kill_permission_error_names_the_env_vars_to_set(monkeypatch):
    monkeypatch.delenv("TMUX_KIT_MCP_KILL_ENABLED", raising=False)
    monkeypatch.delenv("TMUX_KIT_MCP_KILL_ALLOW", raising=False)
    with pytest.raises(PermissionError, match="TMUX_KIT_MCP_KILL_ENABLED"):
        await mcp_server.kill("demo")
