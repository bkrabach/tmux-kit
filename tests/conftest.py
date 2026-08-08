"""Safety rails for tmux-kit's own test suite.

This repo has no live production instance to protect (unlike muxplex,
which serves ~67 real tmux sessions on a developer box) -- but its
integration/differential tests still spawn real tmux subprocesses, and a
contributor running the suite on a machine that also has an ambient tmux
server (or a `TMUX_TMPDIR` exported into their shell rc) must never have
those calls land on it by accident. See AGENTS.md ("Any test or proof
that arms this hook for real must run against an isolated tmux server --
never the ambient one") in the muxplex repo for the incident this
convention exists to prevent; the rail travels with the code because the
hazard is the library's own `build_alert_bell_hook()` / real-tmux tests,
not anything muxplex-specific.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: real tmux subprocess required (isolated socket dir)"
    )
    config.addinivalue_line(
        "markers", "differential: replays recorded real-tmux fixtures byte-identically"
    )


@pytest.fixture(autouse=True)
def _isolate_tmux_socket_dir(monkeypatch: pytest.MonkeyPatch):
    """Force every test's REAL (unmocked) tmux subprocess calls onto an
    isolated, per-test socket directory -- never the ambient default.

    Deliberately NOT built on pytest's own ``tmp_path``: on macOS that
    resolves to a long, deeply-nested path that can blow tmux's
    ``sun_path`` budget once ``/tmux-isolated`` and tmux's own
    ``/tmux-<uid>/<socket-name>`` are stacked on top (see muxplex's
    conftest.py, whose ``_isolate_tmux_socket_dir`` this mirrors, for the
    live incident that shape caused). ``mkdtemp`` directly under ``/tmp``
    stays short on every supported platform.
    """
    tmux_dir = Path(tempfile.mkdtemp(prefix="tmux-kit-isolated-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    try:
        yield
    finally:
        shutil.rmtree(tmux_dir, ignore_errors=True)
