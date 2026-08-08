"""Regression tests for spawn_session()'s env-factory default (0.2.0 fix).

Before 0.2.0, ``env`` defaulted to a bare ``None`` ("always inherit
ambient, ignore any installed factory"), diverging from every other
function in this package (``run_tmux`` and everything built on it) that
consults ``proc.set_env_factory()`` when ``env`` is omitted. A consumer
who installed a factory and then called ``spawn_session(name, template)``
with no ``env=`` got a session created on the AMBIENT socket while a
subsequent ``enumerate_sessions()`` (which DOES consult the factory)
looked for it elsewhere -- "still running: []" followed by a "no server
running" RuntimeError, the exact failure that motivated this whole
change. See CHANGELOG (0.2.0) and ``tmux_kit/api.py``'s module docstring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from tmux_kit import proc
from tmux_kit import spawn as spawn_mod


@pytest.fixture(autouse=True)
def _reset_env_factory():
    proc.set_env_factory(None)
    yield
    proc.set_env_factory(None)


def _mock_proc(returncode: int = 0):
    p = MagicMock()
    p.returncode = returncode
    p.communicate = AsyncMock(return_value=(b"", b""))
    return p


async def _spawn_and_capture_env(monkeypatch, **spawn_kwargs):
    monkeypatch.setattr(spawn_mod, "should_escape", AsyncMock(return_value=False))
    monkeypatch.setattr(spawn_mod, "enumerate_sessions", AsyncMock(return_value=["x"]))
    captured: dict = {}

    async def fake_create_subprocess_shell(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return _mock_proc(0)

    monkeypatch.setattr("asyncio.create_subprocess_shell", fake_create_subprocess_shell)
    await spawn_mod.spawn_session("x", "tmux new-session -d -s {name}", **spawn_kwargs)
    return captured["env"]


async def test_env_omitted_with_no_factory_inherits_ambient(monkeypatch):
    """No factory installed, env omitted -> None (inherit ambient) --
    byte-identical to the pre-0.2.0 behavior for every existing caller
    that never installs a factory (e.g. muxplex, which always passes
    env= explicitly anyway)."""
    env = await _spawn_and_capture_env(monkeypatch)
    assert env is None


async def test_env_omitted_with_factory_installed_now_consults_it(monkeypatch):
    """THE FIX: a factory installed via set_env_factory(), with env
    omitted at the spawn_session() call site, must be consulted -- not
    silently ignored."""
    proc.set_env_factory(lambda: {"TMUX_TMPDIR": "/scratch/socket-dir"})
    env = await _spawn_and_capture_env(monkeypatch)
    assert env == {"TMUX_TMPDIR": "/scratch/socket-dir"}


async def test_explicit_env_none_still_means_inherit_ambient(monkeypatch):
    """An explicit env=None must still mean 'inherit ambient, explicitly'
    -- even with a factory installed. Only OMITTING env consults the
    factory; this is what keeps run_tmux()'s existing env=None semantics
    (and every caller that relies on them) unchanged."""
    proc.set_env_factory(lambda: {"TMUX_TMPDIR": "/scratch/socket-dir"})
    env = await _spawn_and_capture_env(monkeypatch, env=None)
    assert env is None


async def test_explicit_env_dict_is_used_verbatim_over_the_factory(monkeypatch):
    proc.set_env_factory(lambda: {"TMUX_TMPDIR": "/scratch/socket-dir"})
    env = await _spawn_and_capture_env(monkeypatch, env={"FOO": "bar"})
    assert env == {"FOO": "bar"}
