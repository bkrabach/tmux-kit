"""Tests for tmux_kit.isolation -- the throwaway-isolated-server primitive.

0.2.1: added after a real incident (see AGENTS.md, "TMUX_TMPDIR is not an
isolation boundary") in which an agent believed setting ``TMUX_TMPDIR`` was
enough to isolate its tmux probing from the operator's real server. It was
not: the agent's shell was itself running inside a tmux pane, so ``$TMUX``
was set, and tmux prefers an inherited ``$TMUX`` over ``TMUX_TMPDIR`` when no
explicit ``-L``/``-S`` is given. `tmux list-sessions` printed 73 real
sessions; `tmux kill-server` destroyed all of them.

``test_isolated_tmux_server_ignores_a_fake_ambient_tmux_env`` below
reproduces the EXACT mechanism -- a session "pretending" to be an attached
tmux client via a crafted ``$TMUX`` -- entirely against two throwaway,
self-created servers. It never touches any real/ambient/production tmux
server on the machine running this suite.

0.2.2: the section below headed "socket path length" was added after
`test-macos` CI failed with ``File name too long`` -- the private
``TMUX_TMPDIR`` this module creates was anchored under macOS's long,
per-user ``/var/folders/.../T`` temp dir, blowing the 104-byte AF_UNIX
``sun_path`` limit once tmux appended its own ``tmux-<uid>/<socket-name>``
suffix. These tests assert the fix (anchoring under ``/tmp`` instead) holds
as a byte-length invariant -- deliberately checked against the *tighter*
macOS bound regardless of which platform is actually running the suite, so
a regression introduced/tested only on Linux (108-byte limit, easily
satisfied by an unrelated future change) cannot slip through invisibly and
only blow up the next time `test-macos` runs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tmux_kit.isolation import (
    _MACOS_UNIX_SOCKET_PATH_LIMIT,
    _MAX_SOCKET_PATH_BYTES,
    IsolatedTmuxServer,
    _scrubbed_env,
    _short_tmp_base,
    _tmux_socket_path,
    isolated_tmux_server,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# _scrubbed_env -- pure function, no subprocess needed
# ---------------------------------------------------------------------------


def test_scrubbed_env_removes_tmux_and_pins_tmpdir(monkeypatch):
    monkeypatch.setenv("TMUX", "/some/ambient/socket,1234,0")
    monkeypatch.setenv("TMUX_TMPDIR", "/some/other/dir")
    env = _scrubbed_env("/private/isolated/dir")
    assert "TMUX" not in env
    assert env["TMUX_TMPDIR"] == "/private/isolated/dir"
    # Everything else from the real environment is preserved (e.g. PATH),
    # so the subprocess can still find the `tmux` binary.
    assert env.get("PATH") == os.environ.get("PATH")


# ---------------------------------------------------------------------------
# isolated_tmux_server -- real tmux, but always on a throwaway socket
# ---------------------------------------------------------------------------


async def test_isolated_tmux_server_spawn_and_list_round_trip():
    async with isolated_tmux_server() as server:
        await server.run("new-session", "-d", "-s", "probe")
        out = await server.run("list-sessions")
    assert "probe" in out


async def test_isolated_tmux_server_unique_names_per_instance():
    """Two concurrent instances never share a socket name -- the
    'safe under concurrent use' requirement."""
    async with isolated_tmux_server() as a, isolated_tmux_server() as b:
        assert a.socket_name != b.socket_name
        assert a.socket_dir != b.socket_dir


async def test_isolated_tmux_server_torn_down_on_normal_exit():
    async with isolated_tmux_server() as server:
        await server.run("new-session", "-d", "-s", "will-die")
        dead_handle = IsolatedTmuxServer(
            socket_name=server.socket_name, socket_dir=server.socket_dir
        )
    # Directory removed...
    assert not Path(dead_handle.socket_dir).exists()
    # ...and the server itself is gone -- re-targeting the EXACT same
    # (now-defunct) socket name/dir fails rather than finding a live
    # session.
    with pytest.raises(RuntimeError):
        await dead_handle.run("list-sessions")


async def test_isolated_tmux_server_torn_down_even_on_exception():
    """Teardown must run even when the `async with` body raises -- the
    'guaranteed teardown even on exception' requirement."""
    captured_dir: str | None = None
    with pytest.raises(ValueError, match="boom"):
        async with isolated_tmux_server() as server:
            await server.run("new-session", "-d", "-s", "doomed")
            captured_dir = server.socket_dir
            raise ValueError("boom")
    assert captured_dir is not None
    assert not Path(captured_dir).exists()


async def test_isolated_tmux_server_teardown_is_a_noop_if_never_started():
    """If the body never calls run() (no server ever actually spawned),
    teardown must not raise -- kill-server's RuntimeError is swallowed."""
    async with isolated_tmux_server() as server:
        pass
    assert not Path(server.socket_dir).exists()


async def test_isolated_tmux_server_ignores_a_fake_ambient_tmux_env(monkeypatch):
    """Reproduces the exact incident mechanism, entirely against two
    throwaway servers -- never a real/ambient one.

    A "fake ambient" server stands in for the operator's real, attached
    tmux server. We craft a `$TMUX` value that looks like what tmux sets for
    an attached client, pointing at the fake ambient server's socket, and
    confirm `isolated_tmux_server()` still lands on ITS OWN private,
    unrelated socket -- never the one named by the crafted `$TMUX` -- and
    that killing the isolated server never touches the fake ambient one.
    """
    async with isolated_tmux_server(prefix="fake-ambient") as fake_ambient:
        await fake_ambient.run("new-session", "-d", "-s", "real-operator-session")

        # Craft a $TMUX value that looks exactly like what tmux itself sets
        # for an attached client: "<socket-path>,<pid>,<window>". This is
        # the precise condition that misled the incident's probe script.
        fake_tmux_socket_path = (
            f"{fake_ambient.socket_dir}/tmux-{os.getuid()}/{fake_ambient.socket_name}"
        )
        monkeypatch.setenv("TMUX", f"{fake_tmux_socket_path},99999,0")

        async with isolated_tmux_server() as isolated:
            assert isolated.socket_name != fake_ambient.socket_name
            await isolated.run("new-session", "-d", "-s", "isolated-only")
            out = await isolated.run("list-sessions")
            assert "real-operator-session" not in out
            assert "isolated-only" in out

        # The fake ambient server survived untouched -- its session is
        # still there, killing the isolated server did not reach it.
        survivor = await fake_ambient.run("list-sessions")
        assert "real-operator-session" in survivor


# ---------------------------------------------------------------------------
# Socket path length -- the 0.2.2 macOS `File name too long` regression
# ---------------------------------------------------------------------------


def test_short_tmp_base_returns_an_existing_short_directory():
    base = _short_tmp_base()
    assert Path(base).is_dir()
    # Not a hard contract on the exact value (falls back to
    # tempfile.gettempdir() if /tmp genuinely doesn't exist), but on any
    # POSIX box running this suite it must be "/tmp" -- the whole point is
    # to avoid macOS's long per-user $TMPDIR.
    assert base == "/tmp"


def test_tmux_socket_path_matches_tmux_own_construction():
    """Pure function, no subprocess: mirrors tmux's own
    ``<TMUX_TMPDIR>/tmux-<uid>/<socket-name>`` path construction, which is
    what the module's own docstring and the incident's error message both
    confirm tmux actually does.
    """
    path = _tmux_socket_path("/tmp/some-dir", "some-socket")
    assert path == f"/tmp/some-dir/tmux-{os.getuid()}/some-socket"


async def test_default_prefix_socket_path_stays_within_macos_sun_path_bound():
    """The regression guard: with the *default* prefix, the real socket
    path this module constructs must stay comfortably under the safe bound
    -- checked against the tighter macOS limit unconditionally, so this
    fails on Linux too if a future change quietly regresses it, instead of
    only surfacing the next time `test-macos` happens to run.
    """
    async with isolated_tmux_server() as server:
        path = _tmux_socket_path(server.socket_dir, server.socket_name)
        path_bytes = len(path.encode("utf-8"))
        assert path_bytes <= _MAX_SOCKET_PATH_BYTES, (
            f"{path!r} is {path_bytes} bytes, over the "
            f"{_MAX_SOCKET_PATH_BYTES}-byte safe bound "
            f"(macOS sun_path cap is {_MACOS_UNIX_SOCKET_PATH_LIMIT})"
        )


async def test_overlong_prefix_raises_before_ever_touching_tmux():
    """A `prefix=` long enough to blow the bound fails loud with an
    actionable ValueError at context-manager entry -- never a cryptic
    tmux-subprocess ``File name too long`` three layers down."""
    with pytest.raises(ValueError, match="exceeding"):
        async with isolated_tmux_server(prefix="x" * 200):
            pytest.fail("must raise before yielding -- body should never run")
