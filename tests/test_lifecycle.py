"""Unit tests for tmux_kit.lifecycle -- kill_session() / interrupt_session().

Spawn's missing counterpart (0.2.0): before this, a consumer had to drop
to ``proc.run_tmux("kill-session", ...)`` by hand to end a session. See
CONSUMERS.md's "NOT in the library yet" for what this deliberately does
NOT add (a Sender/SendPolicy authorization layer) -- these are raw,
unguarded primitives, same trust model as ``proc.run_tmux()`` itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import tmux_kit.lifecycle as lifecycle_mod


async def test_kill_session_uses_exact_match_target(monkeypatch):
    mock = AsyncMock(return_value="")
    monkeypatch.setattr(lifecycle_mod, "run_tmux", mock)
    await lifecycle_mod.kill_session("alpha")
    mock.assert_awaited_once_with("kill-session", "-t", "=alpha")


async def test_kill_session_propagates_tmux_refusal(monkeypatch):
    monkeypatch.setattr(
        lifecycle_mod,
        "run_tmux",
        AsyncMock(side_effect=RuntimeError("can't find session")),
    )
    with pytest.raises(RuntimeError):
        await lifecycle_mod.kill_session("missing")


async def test_interrupt_session_sends_ctrl_c_via_the_shared_keys_builder(monkeypatch):
    """0.4.0: `build_send_key_argv()` (which `interrupt_session()` composes)
    now CHAINS `copy-mode -q -t <target>` ahead of `send-keys` via a
    literal `;` argv element, so a pane stuck in copy-mode no longer
    swallows the C-c -- see keys.py's docstring and CHANGELOG's 0.4.0
    entry. `interrupt_session()` needed no code change of its own to pick
    this up; it inherits the fix purely by composing `build_send_key_argv`.
    """
    mock = AsyncMock(return_value="")
    monkeypatch.setattr(lifecycle_mod, "run_tmux", mock)
    await lifecycle_mod.interrupt_session("alpha")
    mock.assert_awaited_once_with(
        "copy-mode", "-q", "-t", "alpha", ";", "send-keys", "-t", "alpha", "C-c"
    )


async def test_interrupt_session_propagates_tmux_refusal(monkeypatch):
    monkeypatch.setattr(
        lifecycle_mod,
        "run_tmux",
        AsyncMock(side_effect=RuntimeError("can't find session")),
    )
    with pytest.raises(RuntimeError):
        await lifecycle_mod.interrupt_session("missing")
