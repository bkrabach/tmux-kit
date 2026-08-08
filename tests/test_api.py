"""Unit tests for tmux_kit.api -- the 0.2.0 facade.

Every low-level call this module makes is mocked here (this is a unit
suite for the facade's OWN wiring/composition logic -- the real-tmux proof
that the facade actually works end-to-end is the gate example in
examples/quickstart_start.py + quickstart_read.py, run manually per this
change's report). ``configure()``/``doctor()`` touch the filesystem (the
socket directory) and the environment factory (process-global state), so
every test isolates both via the autouse fixture below -- never the real
``~/.local/state/tmux-kit``.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tmux_kit import api, proc


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Every test in this file gets its own scratch socket dir (never the
    real default) and starts/ends with no env factory installed."""
    monkeypatch.setenv("TMUX_KIT_SOCKET_DIR", str(tmp_path / "default-sockets"))
    proc.set_env_factory(None)
    yield
    proc.set_env_factory(None)


# ---------------------------------------------------------------------------
# default_socket_dir() resolution order
# ---------------------------------------------------------------------------


def test_default_socket_dir_env_var_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-sockets"
    monkeypatch.setenv("TMUX_KIT_SOCKET_DIR", str(custom))
    assert api.default_socket_dir() == custom


def test_default_socket_dir_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.delenv("TMUX_KIT_SOCKET_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert api.default_socket_dir() == tmp_path / "tmux-kit" / "sockets"


def test_default_socket_dir_falls_back_to_local_state(monkeypatch):
    monkeypatch.delenv("TMUX_KIT_SOCKET_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    result = api.default_socket_dir()
    assert result == Path("~/.local/state").expanduser() / "tmux-kit" / "sockets"


def test_default_socket_dir_is_not_the_tmux_ambient_default(monkeypatch):
    """Deliberately distinct from tmux's own default -- see the module
    docstring's 'Two apps, one tmux server' hazard."""
    monkeypatch.delenv("TMUX_KIT_SOCKET_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    result = api.default_socket_dir()
    assert "tmux-kit" in str(result)
    assert str(result) != "/tmp/tmux-{}/default".format(__import__("os").getuid())


# ---------------------------------------------------------------------------
# configure() / _ensure_wired() wiring
# ---------------------------------------------------------------------------


def test_configure_creates_dir_and_installs_a_working_factory(tmp_path):
    target = tmp_path / "explicit-sockets"
    resolved = api.configure(socket_dir=target)
    assert resolved == target
    assert target.is_dir()
    factory = proc.get_env_factory()
    assert factory is not None
    env = factory()
    assert env is not None
    assert env["TMUX_TMPDIR"] == str(target)
    assert "TMUX" not in env


def test_configure_default_when_no_socket_dir_given(tmp_path):
    resolved = api.configure()
    assert resolved == tmp_path / "default-sockets"
    assert resolved.is_dir()


def test_ensure_wired_installs_default_when_nothing_installed():
    assert proc.get_env_factory() is None
    api._ensure_wired()
    assert proc.get_env_factory() is not None


def test_ensure_wired_backs_off_when_a_factory_is_already_installed():
    """The whole trick that lets muxplex (which calls set_env_factory()
    itself) and a facade-driven consumer coexist: an already-installed
    factory must be left completely alone."""

    def advanced_consumer_factory():
        return {"TMUX_TMPDIR": "/already/configured/elsewhere"}

    proc.set_env_factory(advanced_consumer_factory)
    api._ensure_wired()
    assert proc.get_env_factory() is advanced_consumer_factory


# ---------------------------------------------------------------------------
# start() -- template construction, especially the shlex.quote() fix
# ---------------------------------------------------------------------------


async def test_start_builds_bare_shell_template_by_default(monkeypatch):
    captured = {}

    async def fake_spawn_session(name, template, **kwargs):
        captured["name"] = name
        captured["template"] = template
        return (True, None)

    monkeypatch.setattr(api.spawn, "spawn_session", fake_spawn_session)
    ok, err = await api.start("demo")
    assert (ok, err) == (True, None)
    assert captured["template"] == "tmux new-session -d -s {name}"


async def test_start_quotes_a_multiword_command_as_one_shell_token(monkeypatch):
    """Regression test for the bug found building this facade's own
    example: an unquoted command containing ';' gets split by the
    WRAPPING shell (spawn_session's own subprocess), not tmux's pane --
    e.g. 'echo hi; sleep 300' would run `sleep 300` in the wrapper,
    blocking spawn_session's 30s call for no reason. shlex.quote() must
    make the whole command reach tmux as ONE argument.
    """
    captured = {}

    async def fake_spawn_session(name, template, **kwargs):
        captured["template"] = template
        return (True, None)

    monkeypatch.setattr(api.spawn, "spawn_session", fake_spawn_session)
    await api.start("demo", "echo hello from tmux-kit; sleep 300", cwd="/tmp/x y")

    tokens = shlex.split(captured["template"])
    assert "echo hello from tmux-kit; sleep 300" in tokens
    assert "-c" in tokens
    assert "/tmp/x y" in tokens


# ---------------------------------------------------------------------------
# list_sessions() / status() / is_running()
# ---------------------------------------------------------------------------


async def test_list_sessions_composes_session_info(monkeypatch):
    async def fake_enumerate():
        return ["a", "b"]

    async def fake_pane_is_dead(name):
        return name == "b"

    monkeypatch.setattr(api.observe, "enumerate_sessions", fake_enumerate)
    monkeypatch.setattr(api.observe, "get_session_activity", lambda: {"a": 1.0})
    monkeypatch.setattr(api.observe, "get_session_created_times", lambda: {"a": 2.0})
    monkeypatch.setattr(api.observe, "get_session_cwds", lambda: {"a": "/tmp"})
    monkeypatch.setattr(api.observe, "pane_is_dead", fake_pane_is_dead)

    sessions = await api.list_sessions()
    by_name = {s.name: s for s in sessions}
    assert by_name["a"].running is True
    assert by_name["a"].activity == 1.0
    assert by_name["a"].cwd == "/tmp"
    assert by_name["b"].running is False
    assert by_name["b"].cwd is None


async def test_list_sessions_treats_a_dead_check_exception_as_not_dead(monkeypatch):
    async def fake_enumerate():
        return ["a"]

    async def raising_pane_is_dead(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(api.observe, "enumerate_sessions", fake_enumerate)
    monkeypatch.setattr(api.observe, "get_session_activity", dict)
    monkeypatch.setattr(api.observe, "get_session_created_times", dict)
    monkeypatch.setattr(api.observe, "get_session_cwds", dict)
    monkeypatch.setattr(api.observe, "pane_is_dead", raising_pane_is_dead)

    sessions = await api.list_sessions()
    assert sessions[0].running is True


async def test_status_missing_running_finished(monkeypatch):
    async def fake_enumerate():
        return ["a"]

    monkeypatch.setattr(api.observe, "enumerate_sessions", fake_enumerate)

    monkeypatch.setattr(api.observe, "pane_is_dead", AsyncMock(return_value=False))
    assert await api.status("a") == "running"

    monkeypatch.setattr(api.observe, "pane_is_dead", AsyncMock(return_value=True))
    assert await api.status("a") == "finished"

    assert await api.status("nope") == "missing"


async def test_is_running_true_only_for_running_status(monkeypatch):
    monkeypatch.setattr(api, "status", AsyncMock(return_value="running"))
    assert await api.is_running("a") is True
    monkeypatch.setattr(api, "status", AsyncMock(return_value="finished"))
    assert await api.is_running("a") is False


# ---------------------------------------------------------------------------
# read()
# ---------------------------------------------------------------------------


async def test_read_delegates_to_capture_pane_with_default_lines(monkeypatch):
    mock = AsyncMock(return_value="captured text")
    monkeypatch.setattr(api.observe, "capture_pane", mock)
    result = await api.read("a")
    assert result == "captured text"
    mock.assert_awaited_once_with("a", api.observe.DEFAULT_CAPTURE_LINES)


# ---------------------------------------------------------------------------
# page() -- the absolute-line-number convenience over capture_pane_window
# ---------------------------------------------------------------------------


async def test_page_default_reads_recent_count(monkeypatch):
    async def fake_metadata(name):
        return (1000, 40, 2000)

    captured = {}

    async def fake_window(name, s, e):
        captured["s"], captured["e"] = s, e
        return (1000, 40, 2000, "line1\nline2\n")

    monkeypatch.setattr(api.observe, "capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(api.observe, "capture_pane_window", fake_window)

    result = await api.page("a", count=50)
    assert captured["s"] == -50
    assert captured["e"] is None
    assert result.returned == 2
    assert result.total == 1040
    assert result.saturated is False


async def test_page_absolute_start_converts_to_relative_coordinates(monkeypatch):
    async def fake_metadata(name):
        return (1000, 40, 2000)

    captured = {}

    async def fake_window(name, s, e):
        captured["s"], captured["e"] = s, e
        return (1000, 40, 2000, "\n".join(["x"] * 10))

    monkeypatch.setattr(api.observe, "capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(api.observe, "capture_pane_window", fake_window)

    result = await api.page("a", start=500, count=100)
    assert captured["s"] == 500 - 1000
    assert captured["e"] == (500 - 1000) + 100 - 1
    assert result.start == 500


async def test_page_reports_saturated_when_history_hits_its_limit(monkeypatch):
    async def fake_metadata(name):
        return (2000, 40, 2000)

    async def fake_window(name, s, e):
        return (2000, 40, 2000, "x\n")

    monkeypatch.setattr(api.observe, "capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(api.observe, "capture_pane_window", fake_window)

    result = await api.page("a", count=10)
    assert result.saturated is True


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_plain_substring_match(monkeypatch):
    async def fake_metadata(name):
        return (0, 3, 0)

    async def fake_window(name, s, e):
        return (0, 3, 0, "line one\nERROR: bad\nline three")

    monkeypatch.setattr(api.observe, "capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(api.observe, "capture_pane_window", fake_window)

    result = await api.search("a", "ERROR")
    assert len(result.matches) == 1
    assert result.matches[0].text == "ERROR: bad"
    assert result.truncated is False


async def test_search_regex_mode(monkeypatch):
    async def fake_metadata(name):
        return (0, 5, 0)

    async def fake_window(name, s, e):
        return (0, 5, 0, "\n".join(f"item{i}" for i in range(5)))

    monkeypatch.setattr(api.observe, "capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(api.observe, "capture_pane_window", fake_window)

    result = await api.search("a", r"item[0-2]$", regex=True)
    assert {m.text for m in result.matches} == {"item0", "item1", "item2"}


async def test_search_truncated_when_history_exceeds_max_lines(monkeypatch):
    async def fake_metadata(name):
        return (10000, 5, 0)

    async def fake_window(name, s, e):
        return (10000, 5, 0, "\n".join(f"item{i}" for i in range(5)))

    monkeypatch.setattr(api.observe, "capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(api.observe, "capture_pane_window", fake_window)

    result = await api.search("a", "item", max_lines=5)
    assert result.truncated is True


async def test_search_truncated_when_match_cap_hit(monkeypatch):
    async def fake_metadata(name):
        return (0, 10, 0)

    async def fake_window(name, s, e):
        return (0, 10, 0, "\n".join(["hit"] * 10))

    monkeypatch.setattr(api.observe, "capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(api.observe, "capture_pane_window", fake_window)

    result = await api.search("a", "hit", max_matches=3)
    assert len(result.matches) == 3
    assert result.truncated is True


# ---------------------------------------------------------------------------
# wait_for_attention() / stop() / kill()
# ---------------------------------------------------------------------------


async def test_wait_for_attention_delegates_to_bell_wait(monkeypatch):
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(api, "wait_for_bell", mock)
    result = await api.wait_for_attention("a", timeout=1.0, poll_interval=0.2)
    assert result is True
    mock.assert_awaited_once_with("a", timeout=1.0, poll_interval=0.2)


async def test_stop_delegates_to_interrupt_session(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(api.lifecycle, "interrupt_session", mock)
    await api.stop("a")
    mock.assert_awaited_once_with("a")


async def test_kill_delegates_to_kill_session(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(api.lifecycle, "kill_session", mock)
    await api.kill("a")
    mock.assert_awaited_once_with("a")


# ---------------------------------------------------------------------------
# rename()
# ---------------------------------------------------------------------------


async def test_rename_rejects_invalid_name_without_calling_tmux(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(api.names, "rename_tmux_session", mock)
    with pytest.raises(ValueError):
        await api.rename("old", "-bad")
    mock.assert_not_awaited()


async def test_rename_rejects_dot_mangled_name_without_calling_tmux(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(api.names, "rename_tmux_session", mock)
    with pytest.raises(ValueError, match="mangles"):
        await api.rename("old", "build.js")
    mock.assert_not_awaited()


async def test_rename_success_reenumerates_and_returns_observed_name(monkeypatch):
    monkeypatch.setattr(api.names, "rename_tmux_session", AsyncMock())
    monkeypatch.setattr(
        api.observe, "enumerate_sessions", AsyncMock(return_value=["new"])
    )
    result = await api.rename("old", "new")
    assert result == "new"


async def test_rename_raises_when_tmux_claims_success_but_name_absent(monkeypatch):
    monkeypatch.setattr(api.names, "rename_tmux_session", AsyncMock())
    monkeypatch.setattr(
        api.observe, "enumerate_sessions", AsyncMock(return_value=["old"])
    )
    with pytest.raises(RuntimeError):
        await api.rename("old", "new")


# ---------------------------------------------------------------------------
# doctor()
# ---------------------------------------------------------------------------


async def test_doctor_reports_tmux_missing(monkeypatch):
    monkeypatch.setattr(api.shutil, "which", lambda name: None)
    monkeypatch.setattr(api.cgroup, "environment_mode", lambda: "not-applicable")

    report = await api.doctor()
    assert report.tmux_found is False
    assert report.tmux_version is None
    assert any("not found" in n for n in report.notes)


async def test_doctor_reports_tmux_found_and_not_applicable_cgroup(monkeypatch):
    class FakeProc:
        async def communicate(self):
            return (b"tmux 3.4\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(api.cgroup, "environment_mode", lambda: "not-applicable")

    report = await api.doctor()
    assert report.tmux_found is True
    assert report.tmux_version == "tmux 3.4"
    assert report.cgroup_mode == "not-applicable"
    assert report.cgroup_escape_ready is False
    assert report.socket_dir_writable is True
    assert any("not-applicable" in n for n in report.notes)


async def test_doctor_notes_when_cgroup_escape_self_test_fails(monkeypatch):
    class FakeProc:
        async def communicate(self):
            return (b"tmux 3.4\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(api.cgroup, "environment_mode", lambda: "scope-candidate")
    monkeypatch.setattr(api.cgroup, "should_escape", AsyncMock(return_value=False))

    report = await api.doctor()
    assert report.cgroup_mode == "scope-candidate"
    assert report.cgroup_escape_ready is False
    assert any("self-test failed" in n for n in report.notes)


async def test_doctor_reports_unwritable_socket_dir(monkeypatch):
    class FakeProc:
        async def communicate(self):
            return (b"tmux 3.4\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(api.cgroup, "environment_mode", lambda: "not-applicable")

    def raising_mkdir(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", raising_mkdir)

    report = await api.doctor()
    assert report.socket_dir_writable is False
    assert any("not writable" in n for n in report.notes)
