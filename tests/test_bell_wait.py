"""Unit tests for tmux_kit.bell's 0.2.0 addition -- wait_for_bell().

``poll_bell_flag()`` answers "has it rung, right now"; ``wait_for_bell()``
answers "block until it rings" -- a real capability gap (agents need to
wait, not poll in a hand-rolled loop). See tmux_kit/api.py's module
docstring for the incident this and the other 0.2.0 additions trace back
to.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import tmux_kit.bell as bell_mod


async def test_wait_for_bell_returns_true_immediately_when_already_rung(monkeypatch):
    monkeypatch.setattr(bell_mod, "poll_bell_flag", AsyncMock(return_value=True))
    result = await bell_mod.wait_for_bell("s1", timeout=5, poll_interval=0.001)
    assert result is True


async def test_wait_for_bell_polls_until_it_rings(monkeypatch):
    calls = {"n": 0}

    async def fake_poll(name):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(bell_mod, "poll_bell_flag", fake_poll)
    result = await bell_mod.wait_for_bell("s1", timeout=5, poll_interval=0.001)
    assert result is True
    assert calls["n"] == 3


async def test_wait_for_bell_returns_false_on_timeout(monkeypatch):
    monkeypatch.setattr(bell_mod, "poll_bell_flag", AsyncMock(return_value=False))
    result = await bell_mod.wait_for_bell("s1", timeout=0.02, poll_interval=0.005)
    assert result is False


async def test_wait_for_bell_only_calls_poll_bell_flag(monkeypatch):
    """No second, parallel tmux interaction -- the entire contract is
    "keep re-checking poll_bell_flag()"."""
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(bell_mod, "poll_bell_flag", mock)
    await bell_mod.wait_for_bell("s1")
    mock.assert_awaited_once_with("s1")


async def test_wait_for_bell_waits_forever_by_default_until_it_rings(monkeypatch):
    calls = {"n": 0}

    async def fake_poll(name):
        calls["n"] += 1
        return calls["n"] >= 5

    monkeypatch.setattr(bell_mod, "poll_bell_flag", fake_poll)
    result = await bell_mod.wait_for_bell("s1", poll_interval=0.001)
    assert result is True
    assert calls["n"] == 5
