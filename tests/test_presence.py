"""Unit tests for tmux_kit.presence -- the session-presence manifest rule.

Carried from muxplex/tests/test_manifest.py (plan §3.2 -- "presence-rule
tests" are named explicitly as an incident test that must not be
stranded). muxplex/tests/test_manifest.py also covers ``load_manifest`` /
``save_manifest`` (file I/O with a muxplex-resolved path), ``get_created_with``
/ ``set_created_with`` (an app-side addition for named session command
pairs), ``get_restore_cwd`` (an app-side accessor), and the rename-journal
functions (``start_rename_journal`` / ``clear_rename_journal``, app-only
bookkeeping) -- none of those are exported by ``tmux_kit.presence`` (see
its module docstring's "Manifest I/O ... deliberately does NOT move at
S1"), so those tests stay in muxplex. Every test below exercises ONLY
``update_manifest`` / ``compute_restore_plan`` / ``mark_restored`` --
the pure functions ``tmux_kit.presence`` actually owns -- against
hand-constructed manifest dicts (no file I/O, no app-side helpers).

See docs/plans/2026-08-08-tmux-lib-extraction-plan.md's
SESSION_PERSISTENCE_DESIGN.md section 5 (in the muxplex repo) for the
full three-way discrimination this rule implements, and this repo's
test_differential_harness.py for the same rule proven against real
fleet-recorded tmux data.
"""

from __future__ import annotations

import json

from tmux_kit.presence import (
    compute_restore_plan,
    mark_restored,
    update_manifest,
)

EPOCH_A = {
    "socket_path": "/home/user/.tmux/tmux-1000/default",
    "server_pid": 111,
    "inode": 1,
}
EPOCH_B = {
    "socket_path": "/home/user/.tmux/tmux-1000/default",
    "server_pid": 222,
    "inode": 2,
}


def _empty_manifest() -> dict:
    return {
        "schema": 2,
        "epoch": None,
        "sessions": {},
        "pending_restore": None,
        "created_with": {},
        "rename_in_flight": None,
    }


# ---------------------------------------------------------------------------
# update_manifest() -- no tmux server available
# ---------------------------------------------------------------------------


def test_update_manifest_no_server_is_unchanged():
    """epoch_now=None (no tmux server) leaves the manifest completely
    untouched. Knowledge is unavailable, not refuted -- must never
    tombstone, never declare a cold start on absence alone.
    """
    manifest = {
        "schema": 1,
        "epoch": EPOCH_A,
        "sessions": {"a2a": {"first_seen_at": 1.0, "last_seen_at": 2.0}},
        "pending_restore": None,
    }
    new_manifest, changed = update_manifest(manifest, None, [])
    assert changed is False
    assert new_manifest is manifest
    assert new_manifest["sessions"] == {
        "a2a": {"first_seen_at": 1.0, "last_seen_at": 2.0}
    }


# ---------------------------------------------------------------------------
# update_manifest() -- first run / adopt
# ---------------------------------------------------------------------------


def test_update_manifest_first_run_adopts_epoch_never_populates_pending_restore():
    """First run ever (manifest.epoch is None) adopts the epoch and
    records live sessions, but NEVER populates pending_restore -- nothing
    can be 'lost' relative to an epoch we've never recorded.
    """
    manifest = _empty_manifest()
    new_manifest, changed = update_manifest(
        manifest, EPOCH_A, ["a2a", "bbs"], now=1000.0
    )
    assert changed is True
    assert new_manifest["epoch"]["socket_path"] == EPOCH_A["socket_path"]
    assert new_manifest["epoch"]["server_pid"] == EPOCH_A["server_pid"]
    assert new_manifest["epoch"]["inode"] == EPOCH_A["inode"]
    assert new_manifest["epoch"]["observed_at"] == 1000.0
    assert set(new_manifest["sessions"]) == {"a2a", "bbs"}
    assert new_manifest["sessions"]["a2a"] == {
        "first_seen_at": 1000.0,
        "last_seen_at": 1000.0,
    }
    assert new_manifest["pending_restore"] is None


# ---------------------------------------------------------------------------
# update_manifest() -- same server (the common, cheap, no-op case)
# ---------------------------------------------------------------------------


def test_update_manifest_same_server_unchanged_sessions_is_a_noop():
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 500.0},
        "sessions": {"a2a": {"first_seen_at": 100.0, "last_seen_at": 100.0}},
        "pending_restore": None,
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_A, ["a2a"], now=600.0)
    assert changed is False
    assert new_manifest["pending_restore"] is None
    assert "a2a" in new_manifest["sessions"]


def test_update_manifest_same_server_new_session_is_recorded():
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 500.0},
        "sessions": {"a2a": {"first_seen_at": 100.0, "last_seen_at": 100.0}},
        "pending_restore": None,
    }
    new_manifest, changed = update_manifest(
        manifest, EPOCH_A, ["a2a", "new-one"], now=700.0
    )
    assert changed is True
    assert "new-one" in new_manifest["sessions"]
    assert new_manifest["sessions"]["new-one"] == {
        "first_seen_at": 700.0,
        "last_seen_at": 700.0,
    }
    assert new_manifest["pending_restore"] is None


def test_update_manifest_same_server_deliberate_kill_is_tombstoned_not_pending():
    """THE sharpest failure mode this design targets: a session killed
    while the poll loop keeps running (same epoch) must be permanently
    removed from the manifest -- NOT queued for restore.
    """
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 500.0},
        "sessions": {
            "a2a": {"first_seen_at": 100.0, "last_seen_at": 100.0},
            "killed-on-purpose": {"first_seen_at": 100.0, "last_seen_at": 100.0},
        },
        "pending_restore": None,
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_A, ["a2a"], now=800.0)
    assert changed is True
    assert "killed-on-purpose" not in new_manifest["sessions"]
    assert new_manifest["pending_restore"] is None, (
        "a deliberate kill against a live, identity-matched server must "
        "NEVER populate pending_restore"
    )


def test_update_manifest_same_server_multiple_deaths_all_tombstoned():
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 500.0},
        "sessions": {
            "keep-me": {"first_seen_at": 1.0, "last_seen_at": 1.0},
            "gone-1": {"first_seen_at": 1.0, "last_seen_at": 1.0},
            "gone-2": {"first_seen_at": 1.0, "last_seen_at": 1.0},
        },
        "pending_restore": None,
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_A, ["keep-me"], now=900.0)
    assert changed is True
    assert set(new_manifest["sessions"]) == {"keep-me"}
    assert new_manifest["pending_restore"] is None


# ---------------------------------------------------------------------------
# update_manifest() -- different server (cold start)
# ---------------------------------------------------------------------------


def test_update_manifest_different_server_populates_pending_restore():
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 100.0},
        "sessions": {
            "a2a": {"first_seen_at": 50.0, "last_seen_at": 100.0},
            "bbs": {"first_seen_at": 60.0, "last_seen_at": 100.0},
        },
        "pending_restore": None,
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_B, [], now=5000.0)
    assert changed is True
    assert new_manifest["epoch"]["server_pid"] == EPOCH_B["server_pid"]
    pending = new_manifest["pending_restore"]
    assert pending is not None
    assert pending["detected_at"] == 5000.0
    assert pending["lost_epoch"]["server_pid"] == EPOCH_A["server_pid"]
    assert set(pending["sessions"]) == {"a2a", "bbs"}
    assert new_manifest["sessions"] == {}


def test_update_manifest_cold_start_pending_restore_is_frozen_not_live():
    """pending_restore must be a FROZEN snapshot: a later same-server poll
    cycle (now running under the NEW epoch) must not tombstone the entries
    just queued for restore.
    """
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 100.0},
        "sessions": {"a2a": {"first_seen_at": 50.0, "last_seen_at": 100.0}},
        "pending_restore": None,
    }
    manifest, changed1 = update_manifest(manifest, EPOCH_B, [], now=5000.0)
    assert changed1 is True
    assert manifest["pending_restore"] is not None
    assert "a2a" in manifest["pending_restore"]["sessions"]

    manifest, changed2 = update_manifest(manifest, EPOCH_B, [], now=5010.0)
    assert changed2 is False
    assert manifest["pending_restore"] is not None
    assert "a2a" in manifest["pending_restore"]["sessions"], (
        "pending_restore must survive subsequent poll cycles under the new "
        "epoch -- it is a frozen snapshot, not a live view"
    )


def test_update_manifest_cold_start_no_lost_sessions_leaves_pending_restore_none():
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 100.0},
        "sessions": {"a2a": {"first_seen_at": 50.0, "last_seen_at": 100.0}},
        "pending_restore": None,
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_B, ["a2a"], now=5000.0)
    assert changed is True  # epoch changed, so the manifest write still happens
    assert new_manifest["pending_restore"] is None


# ---------------------------------------------------------------------------
# Epoch identity -- socket_path is part of equality (scratch-instance safety)
# ---------------------------------------------------------------------------


def test_update_manifest_different_socket_path_is_treated_as_different_server():
    manifest = {
        "schema": 1,
        "epoch": {**EPOCH_A, "observed_at": 100.0},
        "sessions": {"a2a": {"first_seen_at": 50.0, "last_seen_at": 100.0}},
        "pending_restore": None,
    }
    scratch_epoch = {**EPOCH_A, "socket_path": "/tmp/other-scratch/tmux-1000/default"}
    new_manifest, changed = update_manifest(manifest, scratch_epoch, [], now=5000.0)
    assert changed is True
    assert new_manifest["pending_restore"] is not None
    assert "a2a" in new_manifest["pending_restore"]["sessions"]


# ---------------------------------------------------------------------------
# compute_restore_plan() -- the plan is always recomputed against live state
# ---------------------------------------------------------------------------


def _manifest_with_pending(names: list[str], *, detected_at: float = 1000.0) -> dict:
    return {
        "schema": 1,
        "epoch": {**EPOCH_B, "observed_at": detected_at},
        "sessions": {},
        "pending_restore": {
            "detected_at": detected_at,
            "lost_epoch": EPOCH_A,
            "sessions": {
                name: {"first_seen_at": 1.0, "last_seen_at": 2.0} for name in names
            },
        },
    }


def test_compute_restore_plan_no_pending_returns_empty():
    manifest = {"schema": 1, "epoch": EPOCH_A, "sessions": {}, "pending_restore": None}
    assert compute_restore_plan(manifest, []) == []


def test_compute_restore_plan_excludes_already_live_names():
    manifest = _manifest_with_pending(["a2a", "bbs", "ccc"])
    plan = compute_restore_plan(manifest, live_names=["bbs"])
    assert plan == ["a2a", "ccc"]


def test_compute_restore_plan_is_sorted():
    manifest = _manifest_with_pending(["zzz", "aaa", "mmm"])
    assert compute_restore_plan(manifest, live_names=[]) == ["aaa", "mmm", "zzz"]


def test_compute_restore_plan_all_live_is_empty():
    manifest = _manifest_with_pending(["a2a", "bbs"])
    assert compute_restore_plan(manifest, live_names=["a2a", "bbs"]) == []


def test_compute_restore_plan_tombstoned_name_structurally_absent():
    manifest = _manifest_with_pending(["a2a"])
    plan = compute_restore_plan(manifest, live_names=[])
    assert "killed-on-purpose" not in plan
    assert plan == ["a2a"]


# ---------------------------------------------------------------------------
# mark_restored() -- clears successfully-restored names, leaves failures
# ---------------------------------------------------------------------------


def test_mark_restored_removes_given_names():
    manifest = _manifest_with_pending(["a2a", "bbs", "ccc"])
    updated = mark_restored(manifest, {"a2a", "bbs"})
    assert set(updated["pending_restore"]["sessions"]) == {"ccc"}


def test_mark_restored_empties_to_none():
    manifest = _manifest_with_pending(["a2a", "bbs"])
    updated = mark_restored(manifest, {"a2a", "bbs"})
    assert updated["pending_restore"] is None


def test_mark_restored_leaves_unmentioned_names_pending():
    manifest = _manifest_with_pending(["a2a", "bbs"])
    updated = mark_restored(manifest, {"a2a"})
    assert set(updated["pending_restore"]["sessions"]) == {"bbs"}


def test_mark_restored_noop_when_nothing_pending():
    manifest = {"schema": 1, "epoch": EPOCH_A, "sessions": {}, "pending_restore": None}
    updated = mark_restored(manifest, {"a2a"})
    assert updated["pending_restore"] is None


def test_mark_restored_is_pure_does_not_mutate_input():
    manifest = _manifest_with_pending(["a2a", "bbs"])
    original_sessions = dict(manifest["pending_restore"]["sessions"])
    mark_restored(manifest, {"a2a"})
    assert manifest["pending_restore"]["sessions"] == original_sessions


# Note: RESTORE_MAX_AGE_SECONDS is a muxplex app-side constant
# (muxplex/manifest.py), not exported by tmux_kit.presence -- its test
# stays in the muxplex repo.


# ---------------------------------------------------------------------------
# cwd tracking -- restore-fidelity groundwork
# ---------------------------------------------------------------------------


def test_update_manifest_first_run_records_cwd_when_given():
    manifest = _empty_manifest()
    new_manifest, _changed = update_manifest(
        manifest, EPOCH_A, ["a2a"], now=1000.0, cwds={"a2a": "/home/user/dev/a2a"}
    )
    assert new_manifest["sessions"]["a2a"]["cwd"] == "/home/user/dev/a2a"


def test_update_manifest_no_cwds_arg_omits_cwd_key():
    manifest = _empty_manifest()
    new_manifest, _changed = update_manifest(manifest, EPOCH_A, ["a2a"], now=1000.0)
    assert "cwd" not in new_manifest["sessions"]["a2a"]


def test_update_manifest_same_server_updates_cwd_without_signaling_changed():
    manifest = {
        "schema": 2,
        "epoch": {**EPOCH_A, "observed_at": 500.0},
        "sessions": {
            "a2a": {
                "first_seen_at": 100.0,
                "last_seen_at": 100.0,
                "cwd": "/home/user/dev/a2a",
            }
        },
        "pending_restore": None,
        "created_with": {},
    }
    new_manifest, changed = update_manifest(
        manifest, EPOCH_A, ["a2a"], now=600.0, cwds={"a2a": "/home/user/dev/a2a-moved"}
    )
    assert changed is False
    assert new_manifest["sessions"]["a2a"]["cwd"] == "/home/user/dev/a2a-moved"


def test_update_manifest_cwd_missing_this_cycle_keeps_previous_value():
    manifest = {
        "schema": 2,
        "epoch": {**EPOCH_A, "observed_at": 500.0},
        "sessions": {
            "a2a": {
                "first_seen_at": 100.0,
                "last_seen_at": 100.0,
                "cwd": "/home/user/dev/a2a",
            }
        },
        "pending_restore": None,
        "created_with": {},
    }
    new_manifest, _changed = update_manifest(
        manifest, EPOCH_A, ["a2a"], now=600.0, cwds={}
    )
    assert new_manifest["sessions"]["a2a"]["cwd"] == "/home/user/dev/a2a"


def test_cold_start_freezes_last_known_cwd_into_pending_restore():
    manifest = {
        "schema": 2,
        "epoch": {**EPOCH_A, "observed_at": 100.0},
        "sessions": {
            "attention-manager": {
                "first_seen_at": 50.0,
                "last_seen_at": 100.0,
                "cwd": "/home/user",
            }
        },
        "pending_restore": None,
        "created_with": {},
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_B, [], now=5000.0)
    assert changed is True
    pending = new_manifest["pending_restore"]
    assert pending["sessions"]["attention-manager"]["cwd"] == "/home/user"


def test_cold_start_new_epoch_records_cwd_for_freshly_seen_sessions():
    manifest = {
        "schema": 2,
        "epoch": {**EPOCH_A, "observed_at": 100.0},
        "sessions": {},
        "pending_restore": None,
        "created_with": {},
    }
    new_manifest, _changed = update_manifest(
        manifest,
        EPOCH_B,
        ["bootstrap-only"],
        now=5000.0,
        cwds={"bootstrap-only": "/home/user/dev/bootstrap-only"},
    )
    assert (
        new_manifest["sessions"]["bootstrap-only"]["cwd"]
        == "/home/user/dev/bootstrap-only"
    )


# ---------------------------------------------------------------------------
# created_with reap rules -- the library owns these even though the
# created_with FEATURE (set_created_with/get_created_with) is app-side
# ---------------------------------------------------------------------------


def test_same_epoch_tombstone_reaps_created_with():
    manifest = {
        "schema": 2,
        "epoch": EPOCH_A,
        "sessions": {"gone": {"first_seen_at": 1.0, "last_seen_at": 1.0}},
        "pending_restore": None,
        "created_with": {"gone": "amplifier"},
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_A, [], now=2.0)
    assert changed is True
    assert "gone" not in new_manifest["sessions"]
    assert "gone" not in new_manifest["created_with"]


def test_created_with_survives_before_first_observation():
    manifest = {
        "schema": 2,
        "epoch": EPOCH_A,
        "sessions": {},
        "pending_restore": None,
        "created_with": {"not-yet-live": "amplifier"},
    }
    new_manifest, _changed = update_manifest(manifest, EPOCH_A, [], now=2.0)
    assert new_manifest["created_with"] == {"not-yet-live": "amplifier"}


def test_tmux_unavailable_never_reaps_created_with():
    manifest = {
        "schema": 2,
        "epoch": EPOCH_A,
        "sessions": {"x": {"first_seen_at": 1.0, "last_seen_at": 1.0}},
        "pending_restore": None,
        "created_with": {"x": "amplifier"},
    }
    new_manifest, changed = update_manifest(manifest, None, [], now=2.0)
    assert changed is False
    assert new_manifest is manifest
    assert new_manifest["created_with"] == {"x": "amplifier"}


def test_cold_start_retains_live_and_pending_created_with():
    manifest = {
        "schema": 2,
        "epoch": EPOCH_A,
        "sessions": {
            "still-live": {"first_seen_at": 1.0, "last_seen_at": 1.0},
            "will-be-pending": {"first_seen_at": 1.0, "last_seen_at": 1.0},
        },
        "pending_restore": None,
        "created_with": {
            "still-live": "amplifier",
            "will-be-pending": "scratch",
            "leaked": "amplifier",
        },
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_B, ["still-live"], now=10.0)
    assert changed is True
    assert new_manifest["created_with"] == {
        "still-live": "amplifier",
        "will-be-pending": "scratch",
    }
    assert "leaked" not in new_manifest["created_with"]


def test_created_with_pop_does_not_add_spurious_change():
    manifest = {
        "schema": 2,
        "epoch": EPOCH_A,
        "sessions": {"x": {"first_seen_at": 1.0, "last_seen_at": 1.0}},
        "pending_restore": None,
        "created_with": {"x": "amplifier"},
    }
    _new_manifest, changed = update_manifest(manifest, EPOCH_A, ["x"], now=2.0)
    assert changed is False


# ---------------------------------------------------------------------------
# S4: the unknown-key round-trip contract
#
# The library owns the CORE top-level keys (schema, epoch, sessions,
# pending_restore, created_with); an app writes its own top-level keys
# BESIDE them. An app-owned key the library has never heard of must
# survive EVERY function that rebuilds or rewrites the manifest --
# verbatim, on every branch.
# ---------------------------------------------------------------------------

APP_KEY = "app_custom_marker"
APP_VALUE = {"owner": "some-second-app", "nested": {"n": 1}, "flag": None}


def _adopted_manifest_with_app_key() -> dict:

    return {
        "schema": 2,
        "epoch": {**EPOCH_A, "observed_at": 100.0},
        "sessions": {
            "alpha": {"first_seen_at": 100.0, "last_seen_at": 100.0},
            "beta": {"first_seen_at": 100.0, "last_seen_at": 100.0},
        },
        "pending_restore": None,
        "created_with": {},
        APP_KEY: json.loads(json.dumps(APP_VALUE)),
    }


def _assert_app_key_verbatim(result: dict):

    assert APP_KEY in result, "app-owned top-level key was dropped"
    assert json.dumps(result[APP_KEY], sort_keys=True) == json.dumps(
        APP_VALUE, sort_keys=True
    ), "app-owned key survived but not verbatim"


def test_s4_update_manifest_changed_same_epoch_cycle_round_trips_app_key():
    manifest = _adopted_manifest_with_app_key()
    new_manifest, changed = update_manifest(
        manifest, EPOCH_A, ["alpha", "gamma"], now=200.0
    )
    assert changed is True  # gamma added AND beta tombstoned
    assert "gamma" in new_manifest["sessions"]
    assert "beta" not in new_manifest["sessions"]
    _assert_app_key_verbatim(new_manifest)


def test_s4_update_manifest_quiet_same_epoch_cycle_round_trips_app_key():
    manifest = _adopted_manifest_with_app_key()
    new_manifest, changed = update_manifest(
        manifest, EPOCH_A, ["alpha", "beta"], now=200.0
    )
    assert changed is False
    _assert_app_key_verbatim(new_manifest)


def test_s4_update_manifest_first_run_adoption_round_trips_app_key():

    manifest = {
        "schema": 2,
        "epoch": None,
        "sessions": {},
        "pending_restore": None,
        "created_with": {},
        APP_KEY: json.loads(json.dumps(APP_VALUE)),
    }
    new_manifest, changed = update_manifest(manifest, EPOCH_A, ["alpha"], now=200.0)
    assert changed is True
    assert new_manifest["sessions"]["alpha"]["first_seen_at"] == 200.0
    _assert_app_key_verbatim(new_manifest)


def test_s4_update_manifest_cold_start_round_trips_app_key():
    manifest = _adopted_manifest_with_app_key()
    new_manifest, changed = update_manifest(manifest, EPOCH_B, ["alpha"], now=200.0)
    assert changed is True
    assert "beta" in new_manifest["pending_restore"]["sessions"]  # frozen
    _assert_app_key_verbatim(new_manifest)


def test_s4_update_manifest_epoch_none_round_trips_app_key():
    manifest = _adopted_manifest_with_app_key()
    new_manifest, changed = update_manifest(manifest, None, [], now=200.0)
    assert changed is False
    _assert_app_key_verbatim(new_manifest)


def test_s4_mark_restored_round_trips_app_key():
    manifest = _manifest_with_pending(["a2a"])

    manifest[APP_KEY] = json.loads(json.dumps(APP_VALUE))
    _assert_app_key_verbatim(mark_restored(manifest, {"a2a"}))
    no_pending = _adopted_manifest_with_app_key()
    _assert_app_key_verbatim(mark_restored(no_pending, {"anything"}))
