"""Unit + real-tmux tests for capture_pane_metadata()'s failure contract.

The function documents "Raises RuntimeError if tmux/the session is
unreachable (same as `run_tmux`)". `run_tmux()` alone cannot deliver that,
because `display-message -p` does not fail on an unresolvable `-t`: it
exits 0 and expands every `#{...}` to the empty string. The parse is what
breaks, and before this guard it broke as a ValueError.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from unittest.mock import AsyncMock

import pytest
import tmux_kit.observe as observe_mod


async def test_metadata_parses_a_normal_reply(monkeypatch):
    monkeypatch.setattr(
        observe_mod, "run_tmux", AsyncMock(return_value="0\t24\t50000\n")
    )
    assert await observe_mod.capture_pane_metadata("alpha") == (0, 24, 50000)


async def test_unresolvable_target_raises_runtime_error_not_value_error(monkeypatch):
    """tmux's exit-0-with-empty-fields reply is a MISS, not a parse bug.

    Callers are documented to handle RuntimeError here; a ValueError
    escapes their `except RuntimeError` and surfaces two frames up.
    """
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="\t\t\n"))
    with pytest.raises(RuntimeError) as excinfo:
        await observe_mod.capture_pane_metadata("ghost")
    assert "ghost" in str(excinfo.value)


async def test_partial_expansion_also_raises_runtime_error(monkeypatch):
    """Any non-integer field is the same miss -- never a silent zero."""
    monkeypatch.setattr(observe_mod, "run_tmux", AsyncMock(return_value="0\t\t\n"))
    with pytest.raises(RuntimeError):
        await observe_mod.capture_pane_metadata("half-resolved")


async def test_run_tmux_runtime_error_still_propagates(monkeypatch):
    """The pre-existing leg is untouched: a real non-zero exit still
    raises RuntimeError from run_tmux itself."""
    monkeypatch.setattr(
        observe_mod,
        "run_tmux",
        AsyncMock(side_effect=RuntimeError("no server running")),
    )
    with pytest.raises(RuntimeError, match="no server running"):
        await observe_mod.capture_pane_metadata("alpha")


# --- real tmux ---------------------------------------------------------


@pytest.fixture
def tmux_socket():
    """A uniquely-named tmux socket for this test, torn down afterward --
    never the default socket, never the ambient server."""
    name = f"tmux-kit-test-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
    )


async def _run(socket: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "-L",
        socket,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace"))
    return stdout.decode("utf-8", errors="replace")


@pytest.mark.integration
async def test_real_tmux_exits_zero_with_empty_fields_on_a_miss(
    tmux_socket, monkeypatch
):
    """The premise, proven against a real tmux rather than asserted.

    Both misses below exit 0. Neither reaches run_tmux's non-zero leg,
    which is exactly why the parse needs its own guard.
    """

    async def fake_run_tmux(*args: str) -> str:
        return await _run(tmux_socket, *args)

    monkeypatch.setattr(observe_mod, "run_tmux", fake_run_tmux)
    await _run(tmux_socket, "new-session", "-d", "-s", "alpha", "sleep 300")

    # A live session resolves.
    history_size, pane_height, history_limit = await observe_mod.capture_pane_metadata(
        "alpha"
    )
    assert isinstance(history_size, int)
    assert pane_height > 0
    assert history_limit > 0

    # A session that does not exist: tmux exits 0, fields empty.
    with pytest.raises(RuntimeError):
        await observe_mod.capture_pane_metadata("ghost")

    # tmux's exact-match SESSION form is not a valid PANE target, so it
    # misses even though 'alpha' is live and attached to this server.
    with pytest.raises(RuntimeError):
        await observe_mod.capture_pane_metadata("=alpha")
