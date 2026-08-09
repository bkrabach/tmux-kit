"""Pane-harness labeling: the honesty rule and its evidence hierarchy.

Contributed with tmux_kit/labels.py by the second consumer
(concern-sessions). The assertions here encode that consumer's live
counterexamples -- the exact-basename rule exists because a real
``amplifier-attention-manager`` process was observed on its box, and the
narrow snapshot patterns exist because a real amplifier session's
conversation text QUOTED "Claude Code". If one of these fails after a
"simplification", the simplification is the bug.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import uuid

import pytest

from tmux_kit.labels import (
    DEFAULT_PROC_BASENAMES,
    DEFAULT_SNAPSHOT_PATTERNS,
    HARNESS_UNKNOWN,
    HarnessLabel,
    _label_from_process_tree,
    _label_from_snapshot,
    _match_cmdline,
    label_session,
    label_sessions,
    process_table,
)

# --- exact-basename matching (the mislabel guard) ----------------------------


def test_basename_matches_full_path_entrypoint():
    assert (
        _match_cmdline("/home/u/.local/bin/amplifier run", DEFAULT_PROC_BASENAMES)
        == "amplifier"
    )


def test_substring_of_basename_must_not_match():
    # Live counterexample: a real sibling binary on the contributing
    # consumer's box. A substring match would label it amplifier.
    assert (
        _match_cmdline(
            "/home/u/bin/amplifier-attention-manager --watch", DEFAULT_PROC_BASENAMES
        )
        is None
    )


def test_claude_and_codex_entrypoints():
    assert _match_cmdline("node /usr/bin/claude", DEFAULT_PROC_BASENAMES) == (
        "claude-code"
    )
    assert _match_cmdline("codex --full-auto", DEFAULT_PROC_BASENAMES) == "codex"


def test_caller_supplied_basename_table_wins():
    assert _match_cmdline("mytool serve", {"mytool": "my-harness"}) == "my-harness"


# --- executable-position scoping (matcher must not label on ANY argv token) --
# Working exploits against the unscoped matcher this replaced: a token-scan
# labeled routine, unrelated commands purely because a harness name appeared
# ANYWHERE in argv -- e.g. as a git branch, a log path, a filename, an env
# assignment, or a backup source/destination. None of these are the harness.


def test_git_checkout_of_a_branch_named_like_a_harness_does_not_match():
    assert _match_cmdline("git checkout claude", DEFAULT_PROC_BASENAMES) is None


def test_cat_of_a_log_path_containing_a_harness_name_does_not_match():
    assert _match_cmdline("cat /var/log/amplifier", DEFAULT_PROC_BASENAMES) is None


def test_vim_of_a_file_named_like_a_harness_does_not_match():
    assert _match_cmdline("vim /home/user/notes/codex", DEFAULT_PROC_BASENAMES) is None


def test_env_assignment_containing_a_harness_path_does_not_match():
    # The leading token is a shell env-var assignment, not the executable --
    # and its basename (after the last "/") still LOOKS like a real
    # entrypoint, which is exactly the false-positive shape to guard.
    assert (
        _match_cmdline(
            "AMPLIFIER_HOME=/opt/apps/amplifier some_daemon --serve",
            DEFAULT_PROC_BASENAMES,
        )
        is None
    )


def test_rsync_source_and_dest_containing_a_harness_name_does_not_match():
    assert (
        _match_cmdline(
            "rsync -av /data/backups/amplifier /mnt/backup/", DEFAULT_PROC_BASENAMES
        )
        is None
    )


def test_interpreter_direct_script_invocation_still_matches():
    # A harness shipped as a script run via its interpreter directly
    # (no shebang exec) -- the interpreter itself is never the harness,
    # its target argument is.
    assert (
        _match_cmdline("node /usr/bin/claude", DEFAULT_PROC_BASENAMES) == "claude-code"
    )


def test_python_dash_m_module_invocation_still_matches():
    assert _match_cmdline("python -m amplifier", DEFAULT_PROC_BASENAMES) == "amplifier"
    assert (
        _match_cmdline("python3.12 -m amplifier", DEFAULT_PROC_BASENAMES) == "amplifier"
    )


def test_task_runner_run_subcommand_still_matches():
    assert _match_cmdline("uv run amplifier", DEFAULT_PROC_BASENAMES) == "amplifier"


def test_task_runner_without_run_subcommand_does_not_match():
    # "uv" has many subcommands whose positional argument is NOT a program
    # to execute (uv build, uv sync, ...) -- only "run" is special-cased.
    assert _match_cmdline("uv sync amplifier", DEFAULT_PROC_BASENAMES) is None


def test_shell_dash_c_nested_harness_invocation_still_matches():
    assert (
        _match_cmdline("sh -c amplifier serve", DEFAULT_PROC_BASENAMES) == "amplifier"
    )


def test_shell_dash_c_nested_env_assignment_does_not_match():
    # Combines both evasions: shell -c wrapping AND a leading env
    # assignment inside the nested script.
    assert (
        _match_cmdline(
            "sh -c AMPLIFIER_HOME=/opt/apps/amplifier some_daemon --serve",
            DEFAULT_PROC_BASENAMES,
        )
        is None
    )


# --- process-tree BFS (shallowest match owns the pane) ------------------------


def test_root_pane_process_is_checked_first():
    # tmux spawned the harness directly, no shell wrapper (live case):
    # a descendants-only walk would miss it.
    children: dict[int, list[int]] = {}
    cmds = {10: "/home/u/.local/bin/amplifier"}
    hit = _label_from_process_tree(10, children, cmds, DEFAULT_PROC_BASENAMES)
    assert hit is not None and hit[0] == "amplifier"


def test_bfs_shallowest_match_wins():
    # The pane shell runs amplifier, which itself spawned codex as a
    # subprocess -- the pane's OWNER is amplifier.
    children = {1: [2], 2: [3], 3: [4]}
    cmds = {1: "-bash", 2: "amplifier", 3: "codex", 4: "sleep 1"}
    hit = _label_from_process_tree(1, children, cmds, DEFAULT_PROC_BASENAMES)
    assert hit is not None and hit[0] == "amplifier"


def test_no_match_returns_none():
    children = {1: [2]}
    cmds = {1: "-bash", 2: "vim notes.md"}
    assert _label_from_process_tree(1, children, cmds, DEFAULT_PROC_BASENAMES) is None


# --- snapshot sniff (narrow by design) ----------------------------------------


def test_banner_signatures_match():
    hit = _label_from_snapshot(
        "\n Welcome to Claude Code \n>", DEFAULT_SNAPSHOT_PATTERNS
    )
    assert hit is not None and hit[0] == "claude-code"
    hit = _label_from_snapshot("Token Usage (session)", DEFAULT_SNAPSHOT_PATTERNS)
    assert hit is not None and hit[0] == "amplifier"


def test_model_name_in_other_chrome_must_not_match():
    # "claude-opus-5" appearing in amplifier's chrome is NOT Claude Code.
    assert (
        _label_from_snapshot("model: claude-opus-5", DEFAULT_SNAPSHOT_PATTERNS) is None
    )


def test_bare_product_mention_must_not_match():
    # Prose ABOUT a harness is the known false-positive class; only
    # chrome-shaped signatures (banner/version lines) may label.
    assert (
        _label_from_snapshot("let's discuss codex", DEFAULT_SNAPSHOT_PATTERNS) is None
    )


def test_prose_mentioning_a_version_like_phrase_mid_sentence_must_not_match():
    # A real chat message quoting BOTH a version-shaped phrase and a
    # second harness name, mid-sentence -- not a banner. Patterns must be
    # anchored to line-start, or this reads as a claude-code banner.
    assert (
        _label_from_snapshot(
            "what do you think of Claude Code v2 compared to amplifier?",
            DEFAULT_SNAPSHOT_PATTERNS,
        )
        is None
    )


def test_prose_mentioning_a_banner_phrase_mid_sentence_must_not_match():
    assert (
        _label_from_snapshot(
            "Reading the changelog: OpenAI Codex was announced in 2021.",
            DEFAULT_SNAPSHOT_PATTERNS,
        )
        is None
    )


def test_evidence_is_the_matched_line():
    snapshot = "first line\n  Claude Code v2.1 \nlast line"
    hit = _label_from_snapshot(snapshot, DEFAULT_SNAPSHOT_PATTERNS)
    assert hit is not None
    assert hit[1] == "Claude Code v2.1"


# --- the honesty rule, end to end ---------------------------------------------


async def test_label_session_unknown_when_all_sources_silent(monkeypatch):
    import tmux_kit.labels as labels_mod

    async def no_pid(_name):
        return None

    async def empty_snapshot(*_a, **_kw):
        return ""

    monkeypatch.setattr(labels_mod, "pane_pid", no_pid)
    monkeypatch.setattr(labels_mod, "_sniff_snapshot", empty_snapshot)
    result = await label_session("mystery", table=({}, {}))
    assert result == HarnessLabel(
        "mystery",
        HARNESS_UNKNOWN,
        "none",
        "no harness signature in process tree or pane snapshot",
    )


async def test_process_evidence_outranks_snapshot(monkeypatch):
    import tmux_kit.labels as labels_mod

    async def pid_10(_name):
        return 10

    async def claude_banner(*_a, **_kw):  # pragma: no cover - must not be reached
        return "Welcome to Claude Code"

    monkeypatch.setattr(labels_mod, "pane_pid", pid_10)
    monkeypatch.setattr(labels_mod, "_sniff_snapshot", claude_banner)
    result = await label_session("s", table=({}, {10: "amplifier"}))
    assert result.label == "amplifier"
    assert result.source == "process"


async def test_label_sessions_uses_one_table_for_all(monkeypatch):
    import tmux_kit.labels as labels_mod

    calls = {"n": 0}

    async def counting_table():
        calls["n"] += 1
        return {}, {10: "amplifier", 20: "codex"}

    pids = {"a": 10, "b": 20}

    async def fake_pid(name):
        return pids[name]

    monkeypatch.setattr(labels_mod, "process_table", counting_table)
    monkeypatch.setattr(labels_mod, "pane_pid", fake_pid)
    results = await label_sessions(["a", "b"])
    assert calls["n"] == 1
    assert [r.label for r in results] == ["amplifier", "codex"]


# --- real ps / real tmux ------------------------------------------------------


async def test_process_table_reads_the_real_ps():
    children, cmds = await process_table()
    me = os.getpid()
    assert me in cmds  # this test process is visible with a cmdline
    assert isinstance(children, dict)


@pytest.mark.integration
class TestRealTmux:
    """Against an isolated ``tmux -L`` socket (never the ambient server)."""

    @pytest.fixture
    def tmux_socket(self):
        name = f"tmux-kit-labels-{uuid.uuid4().hex[:8]}"
        yield name
        subprocess.run(
            ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
        )

    @pytest.fixture
    def isolated_run_tmux(self, tmux_socket, monkeypatch):
        import tmux_kit.labels as labels_mod

        async def run_tmux_isolated(*args: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "-L",
                tmux_socket,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="replace"))
            return stdout.decode("utf-8", errors="replace")

        monkeypatch.setattr(labels_mod, "run_tmux", run_tmux_isolated)
        return run_tmux_isolated

    async def test_fake_harness_labels_from_live_process_tree(
        self, isolated_run_tmux, tmp_path
    ):
        # An executable literally NAMED amplifier (exact-basename match is
        # the contract; the binary's location is irrelevant).
        fake = tmp_path / "amplifier"
        fake.write_text("#!/bin/sh\nsleep 30\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        await isolated_run_tmux("new-session", "-d", "-s", "labels-integ", str(fake))
        result = await label_session("labels-integ")
        assert result.label == "amplifier"
        assert result.source == "process"
        assert "amplifier" in result.evidence

    async def test_bare_shell_pane_is_honestly_unknown(self, isolated_run_tmux):
        await isolated_run_tmux("new-session", "-d", "-s", "labels-plain", "sh")
        result = await label_session("labels-plain")
        assert result.label == HARNESS_UNKNOWN
        assert result.source == "none"
