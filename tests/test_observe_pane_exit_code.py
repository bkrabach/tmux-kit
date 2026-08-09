"""Unit tests for tmux_kit.observe's 0.3.2 addition -- pane_exit_code().

Answers "did it SUCCEED?" for a finished session -- a real capability gap
identified alongside pane_is_dead() ("is it done?"): tmux already exposes
the fact (#{pane_dead_status}) but nothing in this library had an entry
point for it before this.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import tmux_kit.observe as observe_mod


async def test_pane_exit_code_returns_int_when_dead_with_status(monkeypatch):
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="0\n"))
    assert await observe_mod.pane_exit_code("s1") == 0


async def test_pane_exit_code_returns_nonzero_int(monkeypatch):
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="1\n"))
    assert await observe_mod.pane_exit_code("s1") == 1


async def test_pane_exit_code_none_when_pane_still_running(monkeypatch):
    """tmux leaves #{pane_dead_status} empty while the pane is alive."""
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="\n"))
    assert await observe_mod.pane_exit_code("s1") is None


async def test_pane_exit_code_none_on_runtime_error(monkeypatch):
    """Unknown, not a fact -- a missing session or unreachable tmux must
    never be reported as a specific exit code, matching the same
    convention pane_is_dead()/probe_tmux_epoch() already apply to errors."""
    monkeypatch.setattr(
        observe_mod,
        "run_tmux",
        AsyncMock(side_effect=RuntimeError("can't find session")),
    )
    assert await observe_mod.pane_exit_code("missing") is None


async def test_pane_exit_code_none_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="garbage\n"))
    assert await observe_mod.pane_exit_code("s1") is None


async def test_pane_exit_code_calls_display_message_with_pane_dead_status_format(
    monkeypatch,
):
    mock = AsyncMock(return_value="0\n")
    monkeypatch.setattr(observe_mod, "run_tmux", mock)
    await observe_mod.pane_exit_code("s1")
    mock.assert_awaited_once_with(
        "display-message", "-p", "-t", "s1", "#{pane_dead_status}"
    )
