"""Unit tests for tmux_kit.cli -- the Click CLI over tmux_kit.api.

Skipped entirely if the `cli` extra (click) isn't installed. This repo's
main CI job doesn't install it by default; a separate job does (see
.github/workflows/test.yml) so this file's tests actually execute in CI,
not just locally.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("click")

from click.testing import CliRunner
from tmux_kit import cli as cli_mod


def _runner() -> CliRunner:
    return CliRunner()


def test_help_lists_every_verb():
    result = _runner().invoke(cli_mod.main, ["--help"])
    assert result.exit_code == 0
    for verb in [
        "start",
        "list",
        "status",
        "exit-code",
        "read",
        "page",
        "search",
        "wait",
        "stop",
        "kill",
        "rename",
        "doctor",
    ]:
        assert verb in result.output


def test_start_success(monkeypatch):
    async def fake_start(name, command, *, cwd=None):
        return (True, None)

    monkeypatch.setattr(cli_mod.api, "start", fake_start)
    result = _runner().invoke(cli_mod.main, ["start", "demo"])
    assert result.exit_code == 0
    assert "started 'demo'" in result.output


def test_start_failure_exits_nonzero_with_reason(monkeypatch):
    async def fake_start(name, command, *, cwd=None):
        return (False, "command not found")

    monkeypatch.setattr(cli_mod.api, "start", fake_start)
    result = _runner().invoke(cli_mod.main, ["start", "demo"])
    assert result.exit_code == 1
    assert "command not found" in result.output


def test_start_rejects_mangle_prone_name_exits_nonzero(monkeypatch):
    """api.start() (real, unmocked) raises ValueError for a '.'-containing
    name before ever calling spawn_session -- the CLI catches it and exits
    1 with the reason, same handling as `rename`'s ValueError below."""
    result = _runner().invoke(cli_mod.main, ["start", "build.js"])
    assert result.exit_code == 1
    assert "mangles" in result.output


def test_list_human_and_json(monkeypatch):
    async def fake_list():
        return [
            cli_mod.api.SessionInfo(
                name="a", running=True, activity=1.0, created=2.0, cwd="/tmp"
            )
        ]

    monkeypatch.setattr(cli_mod.api, "list_sessions", fake_list)

    human = _runner().invoke(cli_mod.main, ["list"])
    assert human.exit_code == 0
    assert "a\trunning" in human.output

    as_json = _runner().invoke(cli_mod.main, ["list", "--json"])
    assert as_json.exit_code == 0
    data = json.loads(as_json.output)
    assert data[0]["name"] == "a"
    assert data[0]["running"] is True


def test_status(monkeypatch):
    async def fake_status(name):
        return "finished"

    monkeypatch.setattr(cli_mod.api, "status", fake_status)
    result = _runner().invoke(cli_mod.main, ["status", "a"])
    assert result.exit_code == 0
    assert "finished" in result.output


def test_doctor_human_output(monkeypatch):
    async def fake_doctor():
        return cli_mod.api.DoctorReport(
            tmux_found=True,
            tmux_version="tmux 3.4",
            cgroup_mode="not-applicable",
            cgroup_escape_ready=False,
            socket_dir="/x",
            socket_dir_writable=True,
            notes=["a note"],
        )

    monkeypatch.setattr(cli_mod.api, "doctor", fake_doctor)
    result = _runner().invoke(cli_mod.main, ["doctor"])
    assert result.exit_code == 0
    assert "tmux_found: True" in result.output
    assert "note: a note" in result.output


def test_kill_failure_exits_nonzero(monkeypatch):
    async def fake_kill(name):
        raise RuntimeError("can't find session")

    monkeypatch.setattr(cli_mod.api, "kill", fake_kill)
    result = _runner().invoke(cli_mod.main, ["kill", "missing"])
    assert result.exit_code == 1
    assert "can't find session" in result.output


def test_wait_timeout_exits_nonzero(monkeypatch):
    async def fake_wait(name, *, timeout=None, poll_interval=0.5):
        return False

    monkeypatch.setattr(cli_mod.api, "wait_for_attention", fake_wait)
    result = _runner().invoke(cli_mod.main, ["wait", "a", "--timeout", "0.01"])
    assert result.exit_code == 1
    assert "timeout" in result.output


def test_rename_failure_exits_nonzero(monkeypatch):
    async def fake_rename(old_name, new_name):
        raise ValueError("invalid name")

    monkeypatch.setattr(cli_mod.api, "rename", fake_rename)
    result = _runner().invoke(cli_mod.main, ["rename", "old", "bad name"])
    assert result.exit_code == 1
    assert "invalid name" in result.output
