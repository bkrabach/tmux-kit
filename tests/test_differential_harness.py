"""Differential harness for tmux-kit (carried from muxplex/tests/test_differential_harness.py
per plan §3.2 -- "the differential harness and its recordings ... belong
with the function they test").

See docs/plans/2026-08-08-tmux-lib-extraction-plan.md §8.2 (muxplex repo):
every extraction stage was proven safe by replaying REAL recorded inputs
through the code and asserting the results are byte-identical to the
recorded baseline. This file is the replay half, kept alive post-split as
the standing regression bed for the presence rule and the bell-detection
incident; ``recorded.json`` is fleet-captured data, not a fixture invented
for this repo.

**What changed from the muxplex-repo version, and why:** the original file
tested the SAME recorded inputs against two call shapes side by side --
this library's own pure functions (``tmux_kit.observe`` / ``tmux_kit.bell`` /
``tmux_kit.presence`` / ``tmux_kit.keys``) AND muxplex's app-facing
re-export shims (``muxplex.sessions`` / ``muxplex.bells`` / ``muxplex.manifest``
/ ``muxplex.terminal_input``), to prove the shims were the same objects.
Those app-facade assertions, and the ttyd AF_UNIX lifecycle tests (ttyd is
not part of this library -- see tmux_kit/__init__.py's module docstring),
do not travel: there is no ``muxplex`` package here to shim. Every
assertion below exercises ONLY this library's own public functions against
the same recorded tapes -- the exact byte-identity proof this repo can
still make on its own.
"""

from __future__ import annotations

import copy
import json
import os
import stat as stat_module
from pathlib import Path

import pytest
import tmux_kit.bell as bell_mod
import tmux_kit.keys as keys_mod
import tmux_kit.observe as observe_mod
import tmux_kit.presence as presence_mod
import tmux_kit.proc as proc_mod

pytestmark = pytest.mark.differential

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "differential" / "recorded.json"


def canon(obj) -> str:
    """Canonical JSON form used for byte-identity comparison."""
    return json.dumps(obj, sort_keys=True)


@pytest.fixture(scope="module")
def recorded() -> dict:
    if not FIXTURE_PATH.exists():
        pytest.fail(f"differential fixture missing: {FIXTURE_PATH}")
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_player(tape: list[dict]):
    """Sequential run_tmux replayer.

    Asserts the code under test issues the EXACT argv recorded from the
    real run -- the argv assertion is itself differential coverage of
    run_tmux's call shape.
    """
    entries = iter(tape)

    async def player(*args: str) -> str:
        try:
            entry = next(entries)
        except StopIteration:
            pytest.fail(f"replay divergence: unexpected extra tmux call {list(args)}")
        assert list(args) == entry["args"], (
            f"replay divergence: code issued {list(args)}, "
            f"recorded run issued {entry['args']}"
        )
        if "error" in entry:
            raise RuntimeError(entry["error"])
        return entry["stdout"]

    return player


@pytest.fixture(autouse=True)
def _reset_session_caches(monkeypatch):
    """Each replay starts from empty parser caches, like a fresh process."""
    monkeypatch.setattr(observe_mod, "_session_list", [])
    monkeypatch.setattr(observe_mod, "_snapshots", {})
    monkeypatch.setattr(observe_mod, "_activity", {})
    monkeypatch.setattr(observe_mod, "_created", {})
    monkeypatch.setattr(observe_mod, "_cwds", {})


# ---------------------------------------------------------------------------
# observe: enumerate_sessions
# ---------------------------------------------------------------------------


async def test_enumerate_sessions_replays_real_stdout(recorded, monkeypatch):
    case = recorded["enumerate_sessions"]["real"]
    monkeypatch.setattr(observe_mod, "run_tmux", make_player(case["tape"]))
    names = await observe_mod.enumerate_sessions()
    got = {
        "names": names,
        "activity": observe_mod.get_session_activity(),
        "created": observe_mod.get_session_created_times(),
        "cwds": observe_mod.get_session_cwds(),
    }
    assert canon(got) == canon(case["expected"])


async def test_enumerate_sessions_no_server_returns_empty(recorded, monkeypatch):
    case = recorded["enumerate_sessions"]["no_server"]
    monkeypatch.setattr(observe_mod, "run_tmux", make_player(case["tape"]))
    names = await observe_mod.enumerate_sessions()
    got = {
        "names": names,
        "activity": observe_mod.get_session_activity(),
        "created": observe_mod.get_session_created_times(),
        "cwds": observe_mod.get_session_cwds(),
    }
    assert canon(got) == canon(case["expected"])


async def test_enumerate_sessions_malformed_line_tolerances(recorded, monkeypatch):
    """Real-derived stdout, minimally mutated (mutation documented in the
    fixture) -- tmux cannot be made to emit malformed output on demand, so
    derivation-from-real is the honest form here.
    """
    for case in recorded["enumerate_sessions"]["derived"]:
        stdout_value = case["stdout"]

        async def canned(*args: str, _v=stdout_value) -> str:
            return _v

        monkeypatch.setattr(observe_mod, "run_tmux", canned)
        names = await observe_mod.enumerate_sessions()
        got = {
            "names": names,
            "activity": observe_mod.get_session_activity(),
            "created": observe_mod.get_session_created_times(),
            "cwds": observe_mod.get_session_cwds(),
        }
        assert canon(got) == canon(case["expected"]), case["description"]


# ---------------------------------------------------------------------------
# observe: probe_tmux_epoch
# ---------------------------------------------------------------------------


async def test_probe_tmux_epoch_replays_live_server(recorded, monkeypatch):
    case = recorded["probe_tmux_epoch"]["live"]
    monkeypatch.setattr(observe_mod, "run_tmux", make_player(case["tape"]))

    real_stat = os.stat
    socket_path = case["expected"]["socket_path"]

    def fake_stat(path, *args, **kwargs):
        if str(path) == socket_path:
            return os.stat_result(
                (stat_module.S_IFSOCK, case["inode"], 0, 1, 0, 0, 0, 0, 0, 0)
            )
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    result = await observe_mod.probe_tmux_epoch()
    assert canon(result) == canon(case["expected"])


async def test_probe_tmux_epoch_no_server_returns_none(recorded, monkeypatch):
    case = recorded["probe_tmux_epoch"]["no_server"]
    monkeypatch.setattr(observe_mod, "run_tmux", make_player(case["tape"]))
    assert await observe_mod.probe_tmux_epoch() is None


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


async def test_capture_pane_replays(recorded, monkeypatch):
    case = recorded["capture"]["capture_pane"]
    monkeypatch.setattr(observe_mod, "run_tmux", make_player(case["tape"]))
    assert await observe_mod.capture_pane("alpha") == case["expected"]


async def test_capture_pane_metadata_replays(recorded, monkeypatch):
    case = recorded["capture"]["capture_pane_metadata"]
    monkeypatch.setattr(observe_mod, "run_tmux", make_player(case["tape"]))
    result = await observe_mod.capture_pane_metadata("alpha")
    assert list(result) == case["expected"]


async def test_capture_pane_window_replays(recorded, monkeypatch):
    case = recorded["capture"]["capture_pane_window"]
    monkeypatch.setattr(observe_mod, "run_tmux", make_player(case["tape"]))
    result = await observe_mod.capture_pane_window(
        "alpha", case["args"]["s"], case["args"]["e"]
    )
    assert list(result) == case["expected"]


# ---------------------------------------------------------------------------
# bell: poll_bell_flag (incl. the background-window incident)
# ---------------------------------------------------------------------------


async def test_poll_bell_flag_pre_bell_is_false(recorded, monkeypatch):
    case = recorded["poll_bell_flag"]["pre_bell"]
    monkeypatch.setattr(bell_mod, "run_tmux", make_player(case["tape"]))
    assert await bell_mod.poll_bell_flag("beta") is case["expected"] is False


async def test_poll_bell_flag_sees_background_window_bell(recorded, monkeypatch):
    """The multi-window incident (bells.py:45-56 in the pre-split muxplex
    tree): the bell fired in a real NON-active window; list-windows
    enumerates every window, so the recorded stdout has the belling
    window's '1' even though the active window is '0'.
    """
    case = recorded["poll_bell_flag"]["background_window_bell"]
    flags = case["tape"][0]["stdout"].split()
    assert "1" in flags and "0" in flags, "fixture must show the incident shape"
    monkeypatch.setattr(bell_mod, "run_tmux", make_player(case["tape"]))
    assert await bell_mod.poll_bell_flag("beta") is case["expected"] is True


# ---------------------------------------------------------------------------
# presence: update_manifest -- the §8.2 centerpiece
# ---------------------------------------------------------------------------


def test_update_manifest_replays_every_recorded_cycle(recorded):
    """Replay every real (manifest, epoch, live_names, cwds, now) tuple and
    assert (manifest, changed) byte-identical to the recorded baseline.
    """
    for case in recorded["update_manifest"]["cases"]:
        inputs = copy.deepcopy(case["inputs"])
        result, changed = presence_mod.update_manifest(
            inputs["manifest"],
            inputs["epoch_now"],
            inputs["live_names"],
            now=inputs["now"],
            cwds=inputs["cwds"],
        )
        assert canon(result) == canon(case["expected"]["manifest"]), case["description"]
        assert changed == case["expected"]["changed"], case["description"]


def test_update_manifest_epoch_none_is_a_true_noop(recorded):
    """The epoch_now-None branch returns the manifest UNCHANGED -- beyond
    byte-equality, nothing may be structurally added or dropped.
    """
    case = next(
        c
        for c in recorded["update_manifest"]["cases"]
        if c["inputs"]["epoch_now"] is None
    )
    manifest = copy.deepcopy(case["inputs"]["manifest"])
    result, changed = presence_mod.update_manifest(
        manifest, None, [], now=case["inputs"]["now"], cwds={}
    )
    assert changed is False
    assert canon(result) == canon(case["inputs"]["manifest"])


def test_s4_unknown_toplevel_keys_round_trip_verbatim(recorded):
    """App-owned top-level keys (rename_in_flight, app_extra) ROUND-TRIP
    VERBATIM through a changed same-epoch cycle instead of being dropped
    by a closed-key-set rebuild.
    """
    case = next(
        c
        for c in recorded["update_manifest"]["cases"]
        if c["description"].startswith("S4 CONTRACT")
    )
    inputs = copy.deepcopy(case["inputs"])
    assert "app_extra" in inputs["manifest"], "fixture must carry the unknown key"
    assert inputs["manifest"]["rename_in_flight"] is not None
    result, changed = presence_mod.update_manifest(
        inputs["manifest"],
        inputs["epoch_now"],
        inputs["live_names"],
        now=inputs["now"],
        cwds=inputs["cwds"],
    )
    assert changed is True
    assert canon(result["app_extra"]) == canon(case["inputs"]["manifest"]["app_extra"])
    assert canon(result["rename_in_flight"]) == canon(
        case["inputs"]["manifest"]["rename_in_flight"]
    )
    assert canon(result) == canon(case["expected"]["manifest"])


def test_cold_start_freezes_lost_sessions_verbatim(recorded):
    """The cold-start branch: a lost session's entry -- including its
    observed real cwd -- freezes into pending_restore verbatim, and does
    NOT survive un-frozen in `sessions`.
    """
    case = next(
        c
        for c in recorded["update_manifest"]["cases"]
        if c["description"].startswith("cold start")
    )
    inputs = case["inputs"]
    expected = case["expected"]["manifest"]
    frozen = expected["pending_restore"]["sessions"]
    lost = [n for n in inputs["manifest"]["sessions"] if n not in inputs["live_names"]]
    assert lost, "fixture must actually lose a session across the cold start"
    for name in lost:
        assert canon(frozen[name]) == canon(inputs["manifest"]["sessions"][name])
        assert name not in expected["sessions"]
    assert expected["pending_restore"]["lost_epoch"] == inputs["manifest"]["epoch"]


def test_restore_helpers_replay(recorded):
    rh = recorded["restore_helpers"]
    plan_case = rh["compute_restore_plan"]
    assert (
        presence_mod.compute_restore_plan(
            copy.deepcopy(plan_case["inputs"]["manifest"]),
            plan_case["inputs"]["live_names"],
        )
        == plan_case["expected"]
    )
    mr_case = rh["mark_restored"]
    result = presence_mod.mark_restored(
        copy.deepcopy(mr_case["inputs"]["manifest"]),
        set(mr_case["inputs"]["restored_names"]),
    )
    assert canon(result) == canon(mr_case["expected"])
    # get_restore_cwd is a muxplex app-side accessor over the frozen
    # pending_restore snapshot, not a tmux_kit.presence export -- verify
    # the underlying snapshot shape directly instead.
    cwd_case = rh["get_restore_cwd"]
    manifest = cwd_case["inputs"]["manifest"]
    name = cwd_case["inputs"]["name"]
    pending = manifest.get("pending_restore") or {}
    entry = (pending.get("sessions") or {}).get(name) or {}
    assert entry.get("cwd") == cwd_case["expected"]


# ---------------------------------------------------------------------------
# proc: tmux_env construction (library form only -- the app-facade
# injected form, muxplex.sessions.tmux_env(), lives in the muxplex repo)
# ---------------------------------------------------------------------------


def test_tmux_env_with_socket_dir_overrides_and_pops_tmux(recorded, monkeypatch):
    socket_dir = recorded["tmux_env"]["socket_dir"]
    monkeypatch.setenv("TMUX", "/tmp/fake-ambient-tmux-socket,123,0")
    env = proc_mod.tmux_env(socket_dir)
    assert env is not None
    assert env["TMUX_TMPDIR"] == socket_dir
    assert "TMUX" not in env
    expected = dict(os.environ)
    expected["TMUX_TMPDIR"] = socket_dir
    expected.pop("TMUX", None)
    assert env == expected


def test_tmux_env_unset_returns_none(recorded, monkeypatch):
    expected = recorded["tmux_env"]["expected_when_unset"]
    assert proc_mod.tmux_env("") is expected
    assert proc_mod.tmux_env(None) is expected


# ---------------------------------------------------------------------------
# keys: the terminal-input argv/fence surface
# ---------------------------------------------------------------------------


def test_send_text_argv_replays(recorded):
    for case in recorded["keys"]["send_text"]:
        assert (
            keys_mod.build_send_text_argv(case["name"], case["text"])
            == case["expected"]
        )


def test_send_key_argv_replays(recorded):
    for case in recorded["keys"]["send_key"]:
        assert (
            keys_mod.build_send_key_argv(case["name"], case["key"]) == case["expected"]
        )


def test_allowlist_fence_replays(recorded):
    for case in recorded["keys"]["allowlist"]:
        assert (
            keys_mod.session_matches_allowlist(case["name"], case["patterns"])
            is case["expected"]
        ), case


def test_input_allowed_fence_replays(recorded):
    for case in recorded["keys"]["input_allowed"]:
        assert (
            keys_mod.input_allowed_for_session(case["name"], case["settings"])
            is case["expected"]
        ), case


def test_keys_constants_unchanged(recorded):
    consts = recorded["keys"]["constants"]
    assert sorted(keys_mod.ALLOWED_KEYS) == consts["ALLOWED_KEYS"]
    assert keys_mod.MAX_TEXT_BYTES == consts["MAX_TEXT_BYTES"]
    assert keys_mod.MAX_KEYS == consts["MAX_KEYS"]
