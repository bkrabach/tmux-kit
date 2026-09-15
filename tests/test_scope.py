"""Tests for ``TmuxScope``'s non-owning, socket-pinned observations."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tmux_kit import observe, proc
from tmux_kit.isolation import isolated_tmux_server
from tmux_kit.scope import TmuxScope


def test_scope_rejects_unsafe_socket_paths_and_preserves_explicit_path(monkeypatch):
    for path, message in [
        ("", "empty"),
        ("relative/socket", "absolute"),
        ("/tmp/\x00socket", "NUL"),
    ]:
        with pytest.raises(ValueError, match=message):
            TmuxScope(path)

    monkeypatch.setenv("HOME", "/home/tester")
    scope = TmuxScope("~/socket/../kept")
    assert scope.socket_path == "/home/tester/socket/../kept"
    with pytest.raises(AttributeError):
        scope.socket_path = "/other/socket"  # type: ignore[misc]


async def test_scope_snapshots_supplied_and_default_environment(monkeypatch):
    observed: list[dict[str, str]] = []

    async def fake_run(*args, env):
        observed.append(env)
        return ""

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", fake_run)
    monkeypatch.setenv("SCOPE_DEFAULT", "at-construction")
    default_scope = TmuxScope("/tmp/default.sock")
    monkeypatch.setenv("SCOPE_DEFAULT", "changed-later")

    supplied = {"SCOPE_SUPPLIED": "at-construction", "TMUX": "ambient"}
    supplied_scope = TmuxScope("/tmp/supplied.sock", env=supplied)
    supplied["SCOPE_SUPPLIED"] = "changed-later"
    empty_scope = TmuxScope("/tmp/empty.sock", env={})

    await default_scope._run("list-sessions", operation="test")
    await supplied_scope._run("list-sessions", operation="test")
    await empty_scope._run("list-sessions", operation="test")

    assert observed[0]["SCOPE_DEFAULT"] == "at-construction"
    assert observed[1] == {"SCOPE_SUPPLIED": "at-construction"}
    assert observed[2] == {}


async def test_scope_never_uses_global_factory_and_copies_env_each_call(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def forbidden_factory():
        raise AssertionError("TmuxScope must not consult the global factory")

    async def fake_run(*args, env):
        calls.append((args, env))
        return ""

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", fake_run)
    previous_factory = proc.get_env_factory()
    proc.set_env_factory(forbidden_factory)
    try:
        source_env = {"PATH": "/bin", "VALUE": "original", "TMUX": "ambient"}
        scope = TmuxScope("/tmp/pinned.sock", env=source_env)
        source_env["VALUE"] = "caller mutation"
        await scope._run("list-sessions", operation="test")
        await scope._run("list-sessions", operation="test")
    finally:
        proc.set_env_factory(previous_factory)

    assert [args[:2] for args, _ in calls] == [
        ("-S", "/tmp/pinned.sock"),
        ("-S", "/tmp/pinned.sock"),
    ]
    assert calls[0][1] == {"PATH": "/bin", "VALUE": "original"}
    assert calls[0][1] is not calls[1][1]


async def test_scope_enumerates_with_shared_parser_without_legacy_cache_writes(monkeypatch):
    async def fake_run(*args, env):
        assert args == (
            "-S",
            "/tmp/pinned.sock",
            "list-sessions",
            "-F",
            "#{session_name}\t#{window_activity}\t#{session_created}\t#{pane_current_path}",
        )
        return " alpha\t1.5\t2.5\t/work\nbad\tnot-a-number\tbad\t\n"

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", fake_run)
    monkeypatch.setattr(observe, "_session_list", ["legacy"])
    monkeypatch.setattr(observe, "_snapshots", {"legacy": "snapshot"})
    monkeypatch.setattr(observe, "_activity", {"legacy": 1.0})
    monkeypatch.setattr(observe, "_created", {"legacy": 2.0})
    monkeypatch.setattr(observe, "_cwds", {"legacy": "/legacy"})

    result = await TmuxScope("/tmp/pinned.sock", env={}).enumerate_sessions()

    assert result == (
        ["alpha", "bad"],
        {"alpha": 1.5},
        {"alpha": 2.5},
        {"alpha": "/work"},
    )
    assert observe.get_session_list() == ["legacy"]
    assert observe.get_snapshots() == {"legacy": "snapshot"}
    assert observe.get_session_activity() == {"legacy": 1.0}
    assert observe.get_session_created_times() == {"legacy": 2.0}
    assert observe.get_session_cwds() == {"legacy": "/legacy"}


async def test_scope_capture_uses_exact_session_then_immutable_active_pane(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, env):
        calls.append(args)
        command = args[2]
        if command == "list-panes":
            return "%41\t1\n%42\t0\n"
        if command == "capture-pane":
            return "captured\n"
        if command == "display-message" and ";" not in args:
            return "7\t8\t9\n"
        return "7\t8\t9\nwindow\n"

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", fake_run)
    scope = TmuxScope("/tmp/pinned.sock", env={})

    assert await scope.capture_pane("exact", 12) == "captured\n"
    assert await scope.capture_pane("exact", 12, escapes=False) == "captured\n"
    assert await scope.capture_pane_metadata("exact") == (7, 8, 9)
    assert await scope.capture_pane_window("exact", -4, 2) == (7, 8, 9, "window\n")
    assert await scope.capture_pane_window("exact", -4, None, escapes=False) == (
        7,
        8,
        9,
        "window\n",
    )

    resolution_calls = [call for call in calls if call[2] == "list-panes"]
    assert all(call[3:6] == ("-t", "=exact:", "-F") for call in resolution_calls)
    captures = [call for call in calls if call[2] == "capture-pane"]
    assert captures[0][3:] == ("-e", "-p", "-t", "%41", "-S", "-12")
    assert captures[1][3:] == ("-p", "-t", "%41", "-S", "-12")
    window_calls = [call for call in calls if ";" in call]
    assert window_calls[0].count("%41") == 2
    assert window_calls[0][-4:] == ("-S", "-4", "-E", "2")
    assert window_calls[1][-2:] == ("-S", "-4")


@pytest.mark.parametrize("name", ["", "contains:window", "contains.pane", "nul\x00name"])
async def test_scope_rejects_non_session_targets_before_subprocess(name, monkeypatch):
    async def forbidden_run(*args, env):
        raise AssertionError("invalid target must not invoke tmux")

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", forbidden_run)
    with pytest.raises(ValueError):
        await TmuxScope("/tmp/pinned.sock", env={}).capture_pane(name)


@pytest.mark.parametrize(
    "listing, message",
    [
        ("", "0 active panes"),
        ("%1\t1\n%2\t1\n", "2 active panes"),
        ("not-a-pane\t1\n", "invalid pane id"),
        ("%1\tinvalid\n", "invalid active flag"),
        ("%1\n", "expected pane id"),
    ],
)
async def test_scope_rejects_ambiguous_or_malformed_active_panes(
    listing, message, monkeypatch
):
    async def fake_run(*args, env):
        return listing

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", fake_run)
    with pytest.raises(RuntimeError, match=message):
        await TmuxScope("/tmp/pinned.sock", env={}).capture_pane("exact")


@pytest.mark.parametrize(
    "error",
    [RuntimeError(""), FileNotFoundError("tmux"), PermissionError("denied")],
)
async def test_scope_wraps_subprocess_errors_with_context(error, monkeypatch):
    async def failing_run(*args, env):
        raise error

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", failing_run)
    with pytest.raises(RuntimeError, match=r"socket '/tmp/pinned\.sock'.*test") as caught:
        await TmuxScope("/tmp/pinned.sock", env={})._run("list-sessions", operation="test")
    assert str(caught.value)
    assert caught.value.__cause__ is error
    if not str(error).strip():
        assert "tmux produced no error output" in str(caught.value)


async def test_scope_propagates_cancellation(monkeypatch):
    async def cancelled_run(*args, env):
        raise asyncio.CancelledError()

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", cancelled_run)
    with pytest.raises(asyncio.CancelledError):
        await TmuxScope("/tmp/pinned.sock", env={})._run(
            "list-sessions", operation="test"
        )


async def test_scope_wraps_malformed_metadata_with_socket_and_session(monkeypatch):
    async def fake_run(*args, env):
        if args[2] == "list-panes":
            return "%1\t1\n"
        return "not\tmetadata\n"

    monkeypatch.setattr("tmux_kit.scope.proc.run_tmux", fake_run)
    with pytest.raises(RuntimeError, match=r"socket '/tmp/pinned\.sock'.*'exact'"):
        await TmuxScope("/tmp/pinned.sock", env={}).capture_pane_metadata("exact")


@pytest.mark.integration
async def test_scopes_remain_independent_non_owning_and_strict_after_teardown(
    monkeypatch,
):
    """Exercise the new API only against two helper-owned isolated servers."""
    previous_factory = proc.get_env_factory()
    previous_caches = (
        observe._session_list,
        observe._snapshots,
        observe._activity,
        observe._created,
        observe._cwds,
    )
    sentinel_caches = (
        ["legacy"],
        {"legacy": "snapshot"},
        {"legacy": 1.0},
        {"legacy": 2.0},
        {"legacy": "/legacy"},
    )
    observe._session_list = sentinel_caches[0]
    observe._snapshots = sentinel_caches[1]
    observe._activity = sentinel_caches[2]
    observe._created = sentinel_caches[3]
    observe._cwds = sentinel_caches[4]

    stale_scope: TmuxScope | None = None
    stale_socket: Path | None = None
    try:
        async with isolated_tmux_server(prefix="scope-a") as server_a, isolated_tmux_server(
            prefix="scope-b"
        ) as server_b:
            socket_a = Path(server_a.socket_dir) / f"tmux-{os.getuid()}" / server_a.socket_name
            socket_b = Path(server_b.socket_dir) / f"tmux-{os.getuid()}" / server_b.socket_name
            await server_a.run(
                "new-session",
                "-d",
                "-s",
                "same-name",
                "-x",
                "80",
                "-y",
                "9",
                "-c",
                "/tmp",
                "printf 'scope-a-short-marker\\n'; exec sleep 120",
            )
            await server_b.run(
                "new-session",
                "-d",
                "-s",
                "same-name",
                "-x",
                "80",
                "-y",
                "17",
                "-c",
                "/",
                "printf 'scope-b-marker\\n'; exec sleep 120",
            )

            for _ in range(20):
                short_output, b_output = await asyncio.gather(
                    server_a.run("capture-pane", "-p", "-t", "=same-name:"),
                    server_b.run("capture-pane", "-p", "-t", "=same-name:"),
                )
                if (
                    "scope-a-short-marker" in short_output
                    and "scope-b-marker" in b_output
                ):
                    break
                await asyncio.sleep(0.05)
            assert "scope-a-short-marker" in short_output
            assert "scope-b-marker" in b_output

            scope_a, scope_b = TmuxScope(socket_a), TmuxScope(socket_b)
            stale_scope, stale_socket = scope_a, socket_a
            monkeypatch.setenv("TMUX", "/wrong/socket,1,0")
            installed_factory = lambda: {"PATH": "/does-not-exist"}
            proc.set_env_factory(installed_factory)

            # Before the longer session exists, the old "=same" target resolves
            # the shorter prefix; once it exists, tmux rejects that target as
            # ambiguous and would hide this regression.
            for call in (
                scope_a.capture_pane("same"),
                scope_a.capture_pane_metadata("same"),
                scope_a.capture_pane_window("same", -3, None),
                scope_a.capture_pane("missing"),
                scope_a.capture_pane_metadata("missing"),
                scope_a.capture_pane_window("missing", -3, None),
            ):
                with pytest.raises(RuntimeError, match="socket.*session"):
                    await call
            assert observe._session_list is sentinel_caches[0]
            assert observe._snapshots is sentinel_caches[1]
            assert observe._activity is sentinel_caches[2]
            assert observe._created is sentinel_caches[3]
            assert observe._cwds is sentinel_caches[4]
            assert proc.get_env_factory() is installed_factory

            await server_a.run(
                "new-session",
                "-d",
                "-s",
                "same-name-extra",
                "-x",
                "80",
                "-y",
                "9",
                "-c",
                "/tmp",
                "printf 'scope-a-prefix-marker\\n'; exec sleep 120",
            )
            for _ in range(20):
                prefix_output = await server_a.run(
                    "capture-pane", "-p", "-t", "=same-name-extra:"
                )
                if "scope-a-prefix-marker" in prefix_output:
                    break
                await asyncio.sleep(0.05)
            assert "scope-a-prefix-marker" in prefix_output

            (
                a_listing,
                b_listing,
                a_capture,
                b_capture,
                a_metadata,
                b_metadata,
                a_window,
                b_window,
            ) = await asyncio.gather(
                scope_a.enumerate_sessions(),
                scope_b.enumerate_sessions(),
                scope_a.capture_pane("same-name"),
                scope_b.capture_pane("same-name"),
                scope_a.capture_pane_metadata("same-name"),
                scope_b.capture_pane_metadata("same-name"),
                scope_a.capture_pane_window("same-name", -3, None),
                scope_b.capture_pane_window("same-name", -3, None),
            )
            assert a_listing[0] == ["same-name", "same-name-extra"]
            assert b_listing[0] == ["same-name"]
            assert a_listing[3] == {"same-name": "/tmp", "same-name-extra": "/tmp"}
            assert b_listing[3] == {"same-name": "/"}
            assert "scope-a-short-marker" in a_capture
            assert "scope-a-prefix-marker" not in a_capture
            assert "scope-b-marker" not in a_capture
            assert "scope-b-marker" in b_capture
            assert "scope-a-short-marker" not in b_capture
            assert "scope-a-prefix-marker" not in b_capture
            assert a_metadata[1] == 9
            assert b_metadata[1] == 17
            assert "scope-a-short-marker" in a_window[3]
            assert "scope-a-prefix-marker" not in a_window[3]
            assert "scope-b-marker" in b_window[3]
            assert observe._session_list is sentinel_caches[0]
            assert observe._snapshots is sentinel_caches[1]
            assert observe._activity is sentinel_caches[2]
            assert observe._created is sentinel_caches[3]
            assert observe._cwds is sentinel_caches[4]
            assert proc.get_env_factory() is installed_factory

        assert stale_scope is not None and stale_socket is not None
        assert not stale_socket.exists()
        for call in (
            stale_scope.enumerate_sessions(),
            stale_scope.capture_pane("same-name"),
            stale_scope.capture_pane_metadata("same-name"),
            stale_scope.capture_pane_window("same-name", -3, None),
        ):
            with pytest.raises(RuntimeError):
                await call
        assert not stale_socket.exists()
        assert observe._session_list is sentinel_caches[0]
        assert observe._snapshots is sentinel_caches[1]
        assert observe._activity is sentinel_caches[2]
        assert observe._created is sentinel_caches[3]
        assert observe._cwds is sentinel_caches[4]
        assert proc.get_env_factory() is installed_factory
    finally:
        proc.set_env_factory(previous_factory)
        (
            observe._session_list,
            observe._snapshots,
            observe._activity,
            observe._created,
            observe._cwds,
        ) = previous_caches