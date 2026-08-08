"""Unit tests for tmux_kit.observe's 0.2.0 addition -- pane_is_dead().

Answers "is it done, or still going?" -- a real capability gap identified
while building the 0.2.0 facade (see tmux_kit/api.py's module docstring).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import tmux_kit.observe as observe_mod


async def test_pane_is_dead_true_when_flag_is_one(monkeypatch):
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="1\n"))
    assert await observe_mod.pane_is_dead("s1") is True


async def test_pane_is_dead_false_when_flag_is_zero(monkeypatch):
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="0\n"))
    assert await observe_mod.pane_is_dead("s1") is False


async def test_pane_is_dead_false_on_runtime_error(monkeypatch):
    """Unknown, not dead -- a missing session or unreachable tmux must
    never be reported as 'the pane died', matching the same convention
    probe_tmux_epoch()/enumerate_sessions() already apply to errors."""
    monkeypatch.setattr(
        observe_mod,
        "run_tmux",
        AsyncMock(side_effect=RuntimeError("can't find session")),
    )
    assert await observe_mod.pane_is_dead("missing") is False


async def test_pane_is_dead_calls_display_message_with_pane_dead_format(monkeypatch):
    mock = AsyncMock(return_value="0\n")
    monkeypatch.setattr(observe_mod, "run_tmux", mock)
    await observe_mod.pane_is_dead("s1")
    mock.assert_awaited_once_with("display-message", "-p", "-t", "s1", "#{pane_dead}")
