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


async def test_session_exists_strict_matches_exact_names_and_keeps_all_caches(monkeypatch):
    monkeypatch.setattr(observe, "_session_list", ["cached"])
    monkeypatch.setattr(observe, "_snapshots", {"cached": "snapshot"})
    monkeypatch.setattr(observe, "_activity", {"cached": 1.0})
    monkeypatch.setattr(observe, "_created", {"cached": 2.0})
    monkeypatch.setattr(observe, "_cwds", {"cached": "/cached"})
    monkeypatch.setattr(
        observe, "run_tmux", AsyncMock(return_value="named-extra\nnamed\n")
    )

    assert await observe.session_exists_strict("named") is True
    assert await observe.session_exists_strict("name") is False
    assert observe.get_session_list() == ["cached"]
    assert observe.get_snapshots() == {"cached": "snapshot"}
    assert observe.get_session_activity() == {"cached": 1.0}
    assert observe.get_session_created_times() == {"cached": 2.0}
    assert observe.get_session_cwds() == {"cached": "/cached"}


async def test_session_exists_strict_preserves_omitted_none_and_dict_env(monkeypatch):
    mock = AsyncMock(return_value="named\n")
    monkeypatch.setattr(observe, "run_tmux", mock)

    assert await observe.session_exists_strict("named") is True
    mock.assert_awaited_once_with("list-sessions", "-F", "#{session_name}")

    mock.reset_mock()
    assert await observe.session_exists_strict("named", env=None) is True
    mock.assert_awaited_once_with("list-sessions", "-F", "#{session_name}", env=None)

    environment = {"TMUX_TMPDIR": "/explicit-socket"}
    mock.reset_mock()
    assert await observe.session_exists_strict("named", env=environment) is True
    assert mock.await_args.args == ("list-sessions", "-F", "#{session_name}")
    assert mock.await_args.kwargs["env"] is environment


@pytest.mark.parametrize(
    "failure", [RuntimeError("socket failure"), FileNotFoundError("tmux")]
)
async def test_session_exists_strict_propagates_observation_errors(monkeypatch, failure):
    monkeypatch.setattr(observe, "run_tmux", AsyncMock(side_effect=failure))

    with pytest.raises(type(failure), match=str(failure)):
        await observe.session_exists_strict("named")


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


@pytest.mark.parametrize(
    "output", ["", "0\t24\n", "0\t24\t50000\textra\n", "nope\t24\t50000\n"]
)
async def test_both_metadata_callers_reject_malformed_headers(monkeypatch, output):
    mock = AsyncMock(return_value=output)
    monkeypatch.setattr(observe, "run_tmux", mock)

    with pytest.raises(
        RuntimeError, match=r"target 'ghost': expected three integer fields"
    ):
        await observe.capture_pane_metadata("ghost")

    mock.reset_mock()
    with pytest.raises(
        RuntimeError, match=r"target 'ghost': expected three integer fields"
    ):
        await observe.capture_pane_window("ghost", -30, None)


async def test_both_metadata_callers_share_valid_header_parsing(monkeypatch):
    mock = AsyncMock(return_value="12\t24\t50000\n")
    monkeypatch.setattr(observe, "run_tmux", mock)
    assert await observe.capture_pane_metadata("ghost") == (12, 24, 50000)

    mock.reset_mock()
    mock.return_value = "12\t24\t50000\ncaptured pane text\n"
    assert await observe.capture_pane_window("ghost", -30, None) == (
        12,
        24,
        50000,
        "captured pane text\n",
    )
