"""Failure-aware observation and plain-capture regression coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import tmux_kit.observe as observe


async def test_strict_enumeration_distinguishes_empty_success_from_failure(monkeypatch):
    monkeypatch.setattr(observe, "run_tmux", AsyncMock(return_value=""))
    assert await observe.enumerate_sessions_strict() == []

    monkeypatch.setattr(
        observe, "run_tmux", AsyncMock(side_effect=RuntimeError("no server running"))
    )
    assert await observe.enumerate_sessions() == []
    with pytest.raises(RuntimeError, match="no server"):
        await observe.enumerate_sessions_strict()


async def test_strict_enumeration_propagates_missing_tmux_binary(monkeypatch):
    monkeypatch.setattr(
        observe, "run_tmux", AsyncMock(side_effect=FileNotFoundError("tmux"))
    )
    assert await observe.enumerate_sessions() == []
    with pytest.raises(FileNotFoundError, match="tmux"):
        await observe.enumerate_sessions_strict()


async def test_strict_enumeration_forwards_explicit_environment(monkeypatch):
    environment = {"TMUX_TMPDIR": "/explicit-socket"}
    mock = AsyncMock(return_value="named\t1\t2\t/path\n")
    monkeypatch.setattr(observe, "run_tmux", mock)

    assert await observe.enumerate_sessions_strict(env=environment) == ["named"]
    mock.assert_awaited_once_with(
        "list-sessions",
        "-F",
        "#{session_name}\t#{window_activity}\t#{session_created}\t#{pane_current_path}",
        env=environment,
    )


async def test_strict_capture_distinguishes_empty_success_from_failure(monkeypatch):
    mock = AsyncMock(return_value="")
    monkeypatch.setattr(observe, "run_tmux", mock)
    assert await observe.capture_pane_strict("blank") == ""
    mock.assert_awaited_once_with(
        "capture-pane", "-e", "-p", "-t", "blank", "-S", "-30"
    )

    monkeypatch.setattr(
        observe, "run_tmux", AsyncMock(side_effect=RuntimeError("socket failure"))
    )
    assert await observe.capture_pane("blank") == ""
    with pytest.raises(RuntimeError, match="socket failure"):
        await observe.capture_pane_strict("blank")


async def test_lenient_capture_handles_missing_binary_but_strict_capture_does_not(
    monkeypatch,
):
    monkeypatch.setattr(
        observe, "run_tmux", AsyncMock(side_effect=FileNotFoundError("tmux"))
    )
    assert await observe.capture_pane("blank") == ""
    with pytest.raises(FileNotFoundError, match="tmux"):
        await observe.capture_pane_strict("blank")


async def test_capture_escapes_is_keyword_only_and_defaults_to_rendering(monkeypatch):
    mock = AsyncMock(return_value="text")
    monkeypatch.setattr(observe, "run_tmux", mock)
    assert await observe.capture_pane("pane") == "text"
    mock.assert_awaited_once_with(
        "capture-pane", "-e", "-p", "-t", "pane", "-S", "-30"
    )

    mock.reset_mock()
    assert await observe.capture_pane("pane", escapes=False) == "text"
    mock.assert_awaited_once_with("capture-pane", "-p", "-t", "pane", "-S", "-30")


@pytest.mark.parametrize("output", ["\t\t\n", "0\t\t50000\n", "0\t24\n"])
async def test_metadata_malformed_output_raises_targeted_runtime_error(monkeypatch, output):
    monkeypatch.setattr(observe, "run_tmux", AsyncMock(return_value=output))
    with pytest.raises(RuntimeError, match="target 'ghost'"):
        await observe.capture_pane_metadata("ghost")
