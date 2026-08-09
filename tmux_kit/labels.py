"""Pane-harness detection: what agent harness runs in a tmux session's
active pane -- amplifier, claude-code, codex -- or honestly ``unknown``.

Contributed by tmux-kit's second consumer (concern-sessions, the
"sessions" connector of the concern engine -- the consumer CONSUMERS.md's
"NOT in the library yet" section was holding the 0.x line open for). Its
use case: a box running dozens of real tmux sessions, each hosting
WHATEVER coding tool its human chose; observation and steering must be
harness-agnostic, so the label exists to route, never to gate.

**The honesty rule: no signature -> ``unknown``. Never a guess.** A wrong
label is worse than no label -- a steering layer that believes a pane is
amplifier when it is actually a bare shell will type the wrong dialect
into a live terminal.

Two evidence sources, in strength order:

1. **Process tree** (primary). Walk the pane PID's descendants
   breadth-first; match argv TOKEN BASENAMES exactly against the known
   harness entrypoints. Exact-basename matching is load-bearing:
   ``/.../bin/amplifier`` labels amplifier, while
   ``/.../amplifier-attention-manager`` (a live counterexample on the
   contributing consumer's box) must NOT -- a substring match would
   mislabel it. BFS order means the shallowest match wins: a harness that
   itself spawned another tool's subprocess is still the harness that
   OWNS the pane.

2. **Snapshot sniff** (fallback). Only consulted when the process tree is
   silent (harness exited; only its screen remains). Patterns are
   deliberately narrow: pane text QUOTING a harness name (e.g. a
   conversation about Claude Code inside an amplifier session) is a known
   false-positive class, so bare product names never match -- only
   chrome-shaped signatures (banners, version lines). "Chrome-shaped" is
   enforced structurally, not just by wording: every pattern is anchored
   to the START of a screen line (optional leading whitespace only), so
   prose that merely MENTIONS a harness name mid-line -- "what do you
   think of Claude Code v2 compared to amplifier?", "OpenAI Codex was
   announced in 2021" -- cannot match; a genuine banner IS the line, a
   quote of one is not. Model names like ``claude-opus-5`` appearing in
   another harness's chrome must not match. The residual risk is
   inherent to screen-residue evidence: a pane whose *scrollback quotes*
   a full banner line, alone on its own line, after its harness exited
   can still mislabel -- which is why ``source`` is always reported, so
   a caller can weigh ``"snapshot"`` evidence more skeptically than
   ``"process"`` evidence.

Config is injected, never read (plan section 4.3): the known-harness
tables below are DEFAULTS a caller may replace per call; nothing here
reads a settings file. Every tmux invocation goes through
``tmux_kit.proc.run_tmux`` (the one door); the only non-tmux subprocess
is ``ps``, spawned argv-exec with POSIX-portable flags (``-A -ww -o
pid=`` etc. -- header suppression via the ``=`` form works on both
procps/Linux and BSD/macOS ``ps``, unlike GNU-only ``--no-headers``;
``-ww`` forces unlimited-width ``args`` output on both -- see
:func:`process_table`'s docstring for why this is load-bearing, not
decoration).

Snapshot capture here deliberately omits ``capture-pane -e`` (unlike
``tmux_kit.observe.capture_pane``, which preserves ANSI escapes for
rendering): signature regexes must match VISIBLE text, and a TUI banner
rendered in color would otherwise carry escape bytes mid-phrase.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tmux_kit.proc import run_tmux

# The honest label for "no signature found". Exposed so callers compare
# against a constant, not a magic string.
HARNESS_UNKNOWN = "unknown"

# Exact argv-token basenames -> label. A closed DEFAULT set -- callers
# with additional harnesses pass their own mapping to label_session()
# (mechanism, not policy: the library ships the known world, the caller
# owns the final table). Multiple entrypoints may map to one label
# (amplifier's CLI has shipped under several binary names).
DEFAULT_PROC_BASENAMES: Mapping[str, str] = {
    "amplifier": "amplifier",
    "amplifier-next": "amplifier",
    "amplifier-app-cli": "amplifier",
    "claude": "claude-code",
    "codex": "codex",
}

# Narrow, high-precision screen signatures (see module docstring for why
# narrow is non-negotiable). (label, compiled pattern) pairs, first hit
# wins in order.
#
# Anchored to the START of a screen line (``^\s*``, re.MULTILINE): a real
# banner/version/footer line IS the line, possibly indented -- it is
# never introduced by other prose on the same line. This is what makes
# "chrome-shaped" a real constraint rather than a claim: prose that
# merely MENTIONS a harness mid-sentence ("what do you think of Claude
# Code v2 compared to amplifier?", "OpenAI Codex was announced in 2021")
# cannot match, because the signature text does not begin the line --
# only a genuine banner does.
DEFAULT_SNAPSHOT_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "claude-code",
        re.compile(r"^\s*(?:Welcome to Claude Code|Claude Code v\d)", re.MULTILINE),
    ),
    ("codex", re.compile(r"^\s*(?:OpenAI Codex|Codex CLI v\d)", re.MULTILINE)),
    (
        "amplifier",
        re.compile(
            r"^\s*(?:Token Usage \(|Welcome to Amplifier|Amplifier v\d)",
            re.MULTILINE,
        ),
    ),
)

# Default snapshot depth for the sniff: enough to include a TUI's
# footer/banner chrome even with a few blank lines below it.
DEFAULT_SNIFF_LINES = 60

# Hard ceiling on the sniff depth (mirrors observe.MAX_CAPTURE_LINES'
# rationale: an unbounded capture is CPU/memory proportional to the
# request, against a server the caller is also polling).
MAX_SNIFF_LINES = 2000


@dataclass(frozen=True)
class HarnessLabel:
    """A harness label plus the evidence that earned it.

    ``source`` is part of the contract, not decoration: ``"process"``
    evidence is live truth, ``"snapshot"`` evidence is screen residue a
    caller may treat more skeptically, ``"none"`` accompanies the honest
    ``unknown``.
    """

    session: str
    label: str  # a value from the basename/pattern tables, or HARNESS_UNKNOWN
    source: str  # "process" | "snapshot" | "none"
    evidence: str  # matched cmdline / matched snapshot line / why unknown


async def pane_pid(session_name: str) -> int | None:
    """PID of *session_name*'s active pane process (None if unavailable).

    Plain-name target, same guarantee as ``observe.capture_pane``: tmux
    resolves an exact session name to itself before any prefix match, and
    callers are expected to pass names they observed via enumeration.
    """
    try:
        out = await run_tmux("display-message", "-p", "-t", session_name, "#{pane_pid}")
        return int(out.strip())
    except (RuntimeError, FileNotFoundError, ValueError):
        return None


async def process_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    """One ``ps`` pass -> ``(children-by-ppid, cmdline-by-pid)``.

    Spawned argv-exec (never a shell), with POSIX-portable flags: ``-A``
    (all processes; the POSIX spelling of GNU's ``-e``) and per-column
    ``-o pid=`` header suppression, which both procps (Linux) and BSD
    (macOS) ``ps`` honor -- GNU-only ``--no-headers`` would fail on
    macOS, which this library's CI runs.

    ``-ww`` forces unlimited-width ``args`` output on both procps
    (Linux) and BSD (macOS) ``ps``. Without it, ``ps`` derives its
    column width from the process's CONTROLLING TERMINAL via an ioctl --
    independent of whether *this* command's own stdout is piped. A
    headless CI runner has no controlling terminal (unlimited width by
    default, so truncation never shows up there), but a real
    interactive box -- this library's stated target -- does, and a long
    argv (a deep tmp/session path, a long project directory) gets
    silently cut, chopping off exactly the basename
    ``_match_cmdline`` needs to match. Confirmed against real ``ps`` on
    both a Linux box (procps-ng) and macOS (BSD ps): both accept ``-ww``
    and both truncate ``args=`` to the controlling terminal's width
    without it. Proven by
    ``test_fake_harness_labels_from_live_process_tree`` failing under a
    real controlling terminal and passing once ``-ww`` is added (see
    CHANGELOG).

    Returns empty tables on failure -- callers fall back to the snapshot
    sniff rather than raising (an unreadable process table must not turn
    an observation pass into an exception).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-A",
            "-ww",
            "-o",
            "pid=",
            "-o",
            "ppid=",
            "-o",
            "args=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _stderr = await proc.communicate()
        out = stdout_bytes.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return {}, {}
    children: dict[int, list[int]] = defaultdict(list)
    cmds: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children[ppid].append(pid)
        cmds[pid] = parts[2].strip()
    return children, cmds


# A shell env-var-assignment-shaped token (``VAR=value``). A raw ``ps
# args=`` string can show this as the LEADING token(s) before the actual
# executable (``VAR=x cmd``, or a `sh -c` script embedding one) -- never
# the executable itself. Matching its basename would still be a
# false-positive risk: ``AMPLIFIER_HOME=/opt/apps/amplifier`` splits on
# ``/`` to a bare ``amplifier`` basename, same as a real entrypoint.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# POSIX shells. Two distinct invocation shapes both hand off the
# executable identity to something other than argv[0]: a script FILE as
# a direct positional argument -- the kernel's shebang line runs
# ``#!/bin/sh`` as ``/bin/sh <script-path>``, exactly how a shebang'd
# harness entrypoint is really invoked -- and an inline ``-c <script>``
# string. Both are handled below; neither makes the shell itself the
# harness.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

# Script interpreters commonly invoked with the actual target as a
# direct positional argument (shebang-less invocation: ``node
# /usr/bin/claude``). The interpreter itself is never the harness.
_SCRIPT_INTERPRETERS = frozenset({"node", "deno", "bun", "ruby", "perl"})

# Python, matched separately because it additionally supports ``-m
# <module>`` (``python -m amplifier``), which is not a positional script
# path and must be recognized specifically.
_PYTHON_RE = re.compile(r"^python[23]?(\.\d+)?$")

# Project/task runners whose ``run`` subcommand's argument is the actual
# target being executed, not the runner itself (``uv run amplifier``).
_TASK_RUNNERS = frozenset({"uv", "uvx", "pipx"})
_TASK_RUN_SUBCOMMANDS = frozenset({"run"})


def _executable_tokens(cmdline: str) -> list[str]:
    """Candidate EXECUTABLE-POSITION tokens for *cmdline*, in confidence
    order -- never every whitespace-split token (see module docstring:
    a wrong label is worse than no label).

    ``ps``'s ``args=`` output is a flattened, space-joined argv with
    ambiguous boundaries once a multi-word argument is involved -- this
    is a conservative heuristic over that string, not a shell parser.
    Handles the shapes a real process tree contains: a bare executable
    (``argv[0]``); a shell or interpreter's direct positional target,
    which is how the kernel ACTUALLY invokes a shebang'd script
    (``/bin/sh /path/to/amplifier``, ``node /usr/bin/claude``); Python's
    ``-m <module>`` (``python -m amplifier``); a task-runner subcommand
    (``uv run amplifier``); and a nested shell ``-c`` script
    (``sh -c "amplifier serve"``) -- recursing into the nested script's
    own executable position. A leading ``VAR=value`` shell-assignment
    token is skipped first (never the executable). Anything this can't
    confidently resolve degrades to ``argv[0]`` alone: no match there
    falls through to ``unknown``, the safe direction -- never a guess.
    """
    tokens = cmdline.split()
    i = 0
    while i < len(tokens) and _ENV_ASSIGNMENT_RE.match(tokens[i]):
        i += 1
    if i >= len(tokens):
        return []
    exe = tokens[i]
    exe_base = os.path.basename(exe)
    rest = tokens[i + 1 :]

    if exe_base in _TASK_RUNNERS:
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 1
        if j < len(rest) and rest[j] in _TASK_RUN_SUBCOMMANDS:
            j += 1
            while j < len(rest) and rest[j].startswith("-"):
                j += 1
            if j < len(rest):
                return [exe_base, rest[j]]
        return [exe_base]

    if exe_base in _SHELLS and "-c" in rest:
        c_index = rest.index("-c")
        nested = rest[c_index + 1 :]
        if nested:
            return [exe_base, *_executable_tokens(" ".join(nested))]
        return [exe_base]

    is_python = _PYTHON_RE.match(exe_base) is not None
    if exe_base in _SHELLS or exe_base in _SCRIPT_INTERPRETERS or is_python:
        j = 0
        while j < len(rest):
            tok = rest[j]
            if is_python and tok == "-m" and j + 1 < len(rest):
                return [exe_base, rest[j + 1]]
            if tok.startswith("-"):
                j += 1
                continue
            return [exe_base, tok]
        return [exe_base]

    return [exe_base]


def _match_cmdline(cmdline: str, basenames: Mapping[str, str]) -> str | None:
    """Label for *cmdline* if its EXECUTABLE POSITION's basename is a
    known harness entrypoint -- scoped matching only, never a bare
    substring/token scan across the whole argv (see module docstring and
    :func:`_executable_tokens`)."""
    for exe in _executable_tokens(cmdline):
        base = os.path.basename(exe)
        if base in basenames:
            return basenames[base]
    return None


def _label_from_process_tree(
    root_pid: int,
    children: dict[int, list[int]],
    cmds: dict[int, str],
    basenames: Mapping[str, str],
) -> tuple[str, str] | None:
    """(label, evidence-cmdline) from BFS over *root_pid* AND its descendants.

    The pane process itself is checked first: a pane whose root process IS
    the harness (tmux spawned it directly, no shell wrapper -- a live case
    on the contributing consumer's box) would otherwise be invisible to a
    descendants-only walk. BFS by generation means the shallowest match
    wins.
    """
    root_cmdline = cmds.get(root_pid, "")
    root_label = _match_cmdline(root_cmdline, basenames)
    if root_label is not None:
        return root_label, root_cmdline[:160]
    queue = list(children.get(root_pid, []))
    while queue:
        next_queue: list[int] = []
        for pid in queue:
            cmdline = cmds.get(pid, "")
            label = _match_cmdline(cmdline, basenames)
            if label is not None:
                return label, cmdline[:160]
            next_queue.extend(children.get(pid, []))
        queue = next_queue
    return None


def _label_from_snapshot(
    snapshot: str, patterns: Sequence[tuple[str, re.Pattern[str]]]
) -> tuple[str, str] | None:
    """(label, evidence-line) from the first snapshot pattern that hits."""
    for label, pattern in patterns:
        match = pattern.search(snapshot)
        if match:
            line_start = snapshot.rfind("\n", 0, match.start()) + 1
            line_end = snapshot.find("\n", match.end())
            if line_end == -1:
                line_end = len(snapshot)
            return label, snapshot[line_start:line_end].strip()[:160]
    return None


async def _sniff_snapshot(session_name: str, lines: int) -> str:
    """Plain-text pane capture for signature matching ('' on error).

    Deliberately WITHOUT ``-e``: escape sequences interleaved through a
    colored banner would break mid-phrase signature regexes (see module
    docstring). Rendering callers want ``observe.capture_pane`` instead.
    """
    lines = max(1, min(int(lines), MAX_SNIFF_LINES))
    try:
        return await run_tmux(
            "capture-pane", "-p", "-t", session_name, "-S", f"-{lines}"
        )
    except (RuntimeError, FileNotFoundError):
        return ""


async def label_session(
    session_name: str,
    *,
    basenames: Mapping[str, str] = DEFAULT_PROC_BASENAMES,
    patterns: Sequence[tuple[str, re.Pattern[str]]] = DEFAULT_SNAPSHOT_PATTERNS,
    sniff_lines: int = DEFAULT_SNIFF_LINES,
    table: tuple[dict[int, list[int]], dict[int, str]] | None = None,
) -> HarnessLabel:
    """Label the harness in *session_name*'s active pane, with evidence.

    Process tree first (live truth), snapshot sniff second (screen
    residue), else honestly ``unknown`` -- never a guess.

    *table* accepts a pre-fetched :func:`process_table` result so a
    many-session caller pays for ONE ``ps`` pass (see
    :func:`label_sessions`); omitted, a fresh table is read.
    """
    resolved_table = table if table is not None else await process_table()
    pid = await pane_pid(session_name)
    if pid is not None:
        hit = _label_from_process_tree(pid, *resolved_table, basenames)
        if hit is not None:
            return HarnessLabel(session_name, hit[0], "process", hit[1])
    snapshot = await _sniff_snapshot(session_name, sniff_lines)
    hit = _label_from_snapshot(snapshot, patterns)
    if hit is not None:
        return HarnessLabel(session_name, hit[0], "snapshot", hit[1])
    return HarnessLabel(
        session_name,
        HARNESS_UNKNOWN,
        "none",
        "no harness signature in process tree or pane snapshot",
    )


async def label_sessions(
    session_names: Sequence[str],
    *,
    basenames: Mapping[str, str] = DEFAULT_PROC_BASENAMES,
    patterns: Sequence[tuple[str, re.Pattern[str]]] = DEFAULT_SNAPSHOT_PATTERNS,
    sniff_lines: int = DEFAULT_SNIFF_LINES,
) -> list[HarnessLabel]:
    """Label many sessions off ONE ``ps`` pass.

    Sessions are labeled sequentially (each label is at most one
    ``display-message`` plus one ``capture-pane``); the expensive shared
    input -- the full-system process table -- is read once.
    """
    table = await process_table()
    return [
        await label_session(
            name,
            basenames=basenames,
            patterns=patterns,
            sniff_lines=sniff_lines,
            table=table,
        )
        for name in session_names
    ]
