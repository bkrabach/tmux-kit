"""The facade: tmux-kit's one door in.

Why this module exists (read this before touching it): every function in
``proc``/``spawn``/``observe``/``bell``/``names``/``cgroup``/``lifecycle``
is correct and does exactly what its docstring says -- but a brand-new
consumer wiring them together for the first time has to independently
discover: that config is injected rather than read (``proc.set_env_factory``
+ ``proc.tmux_env(socket_dir)``); *which* socket directory to point at, and
that it must not collide with an ambient/other-app tmux server (see
CONSUMERS.md's "Two apps, one tmux server" hazard); and that omitting
``env=`` on ``spawn_session`` used to silently ignore the installed factory
(fixed in 0.2.0 -- see CHANGELOG). Getting any ONE of those wrong produces
exactly the failure that motivated this module: a session spawned on one
socket, a list call reading another, "no server running" from a process
that spawned the session moments earlier.

This module owns that wiring with a sensible, zero-configuration default
(``default_socket_dir()``), while an advanced consumer -- muxplex, today
the only real one -- keeps using ``tmux_kit.proc.set_env_factory()``
directly and is completely unaffected: ``_ensure_wired()`` installs the
default factory ONLY if nothing has installed one already (see its
docstring). The low-level modules are unchanged and remain fully usable on
their own; this is an additive convenience layer, not a replacement.

**Same vocabulary everywhere.** The verb names here (``start``, ``list_sessions``,
``status``, ``read``, ``page``, ``search``, ``wait_for_attention``, ``stop``,
``kill``, ``rename``, ``doctor``) are reused verbatim by ``tmux_kit.cli``
(the Click CLI) and ``tmux_kit.mcp_server`` (the MCP server) -- both are thin
argument-marshalling wrappers over the exact functions in this file. Fix or
extend a capability HERE; the other two surfaces inherit it automatically.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tmux_kit import cgroup, lifecycle, names, observe, proc, spawn
from tmux_kit.bell import DEFAULT_BELL_POLL_INTERVAL, wait_for_bell

# ---------------------------------------------------------------------------
# Wiring: default socket directory + lazy env-factory installation
# ---------------------------------------------------------------------------

# Overrides default_socket_dir() entirely when set. Escape hatch for a
# consumer that wants the facade's convenience functions but a
# non-default location, without calling configure() in code (e.g. from a
# shell profile, a container's env, or a test harness).
_SOCKET_DIR_ENV_VAR = "TMUX_KIT_SOCKET_DIR"


def default_socket_dir() -> Path:
    """Resolve the facade's default tmux socket directory.

    Resolution order:
      1. ``TMUX_KIT_SOCKET_DIR`` environment variable, if set.
      2. ``$XDG_STATE_HOME/tmux-kit/sockets`` (``~/.local/state`` if
         ``XDG_STATE_HOME`` is unset) -- a persistent, per-user location,
         not a temp directory that vanishes on reboot (tmux sockets
         themselves die with their server regardless; what must persist is
         only the DIRECTORY PATH a consumer keeps pointing at across
         restarts of its own process).

    Deliberately NOT tmux's own ambient default socket
    (``/tmp/tmux-$UID/default``) and NOT muxplex's configured
    ``tmux_socket_dir`` -- see ``tmux_kit/CONSUMERS.md``'s "Two apps, one
    tmux server = silent failures" hazard. A facade-driven consumer gets
    its OWN dedicated socket directory by default, so its sessions can
    never collide with, or be swept into the restore/presence view of, a
    co-installed muxplex (or a second tmux-kit consumer) on the same host.
    Point at a shared server only via an explicit ``configure(socket_dir=...)``
    call, made with full knowledge of that hazard.
    """
    override = os.environ.get(_SOCKET_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(state_home).expanduser() / "tmux-kit" / "sockets"


def configure(*, socket_dir: str | os.PathLike[str] | None = None) -> Path:
    """Explicitly wire the facade's socket directory up front.

    Optional -- every facade function calls ``_ensure_wired()`` internally,
    which lazily installs the same default on first use (see its
    docstring). Call this yourself to use a non-default location, or to
    create the directory and install the factory eagerly (e.g. so a bad
    path fails loudly at startup rather than mid-spawn).

    Creates *socket_dir* (or the resolved default) including parents, then
    installs ``proc.tmux_env(str(resolved))`` as the process-wide env
    factory via ``proc.set_env_factory()`` -- UNCONDITIONALLY, overwriting
    whatever was installed before. Call this only when you intend to own
    the wiring; a library layer that merely wants "the default if nothing
    else is configured" should call ``_ensure_wired()`` instead, not this.

    Returns the resolved, now-existing ``Path``.
    """
    resolved = (
        Path(socket_dir).expanduser()
        if socket_dir is not None
        else default_socket_dir()
    )
    resolved.mkdir(parents=True, exist_ok=True)
    proc.set_env_factory(lambda: proc.tmux_env(str(resolved)))
    return resolved


def _ensure_wired() -> None:
    """Install the default env factory, but ONLY if nothing has installed
    one already.

    This is the entire trick that lets muxplex (which calls
    ``proc.set_env_factory()`` itself, at its own construction time) and a
    facade-driven consumer coexist in the same process without the facade
    silently clobbering a real app's own wiring: ``proc.get_env_factory()``
    returning non-``None`` means someone got there first, and this
    function backs off entirely.
    """
    if proc.get_env_factory() is None:
        configure()


# ---------------------------------------------------------------------------
# Data shapes returned by the facade
# ---------------------------------------------------------------------------


@dataclass
class SessionInfo:
    """One row of ``list_sessions()`` output."""

    name: str
    running: bool
    activity: float | None
    created: float | None
    cwd: str | None


@dataclass
class PageResult:
    """One page of scrollback, per ``page()``."""

    text: str
    start: int
    total: int
    returned: int
    saturated: bool


@dataclass
class SearchMatch:
    line: int
    text: str


@dataclass
class SearchResult:
    matches: list[SearchMatch]
    truncated: bool


@dataclass
class DoctorReport:
    """``doctor()``'s "will this work here?" preflight report."""

    tmux_found: bool
    tmux_version: str | None
    cgroup_mode: Literal["scope-candidate", "not-applicable"]
    cgroup_escape_ready: bool
    socket_dir: str
    socket_dir_writable: bool
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lifecycle: start / stop / kill / rename
# ---------------------------------------------------------------------------


# Best-effort, bounded budget for start()'s post-spawn readiness wait (see
# _wait_for_pane_ready()'s docstring). Deliberately small: this narrows the
# empty-read-immediately-after-start race for the common case (a quick
# command that prints something within a few hundred milliseconds -- e.g.
# this library's own quickstart example), it does not and cannot close it
# for a genuinely slow-starting command (a dev server, a large compile).
_START_READY_POLL_BUDGET = 0.5
_START_READY_POLL_INTERVAL = 0.05


async def _wait_for_pane_ready(
    name: str,
    *,
    budget: float = _START_READY_POLL_BUDGET,
    interval: float = _START_READY_POLL_INTERVAL,
) -> None:
    """Best-effort, TIME-BOUNDED wait for *name*'s pane to either show its
    first output or have its foreground command already exit -- whichever
    happens first -- so an immediate ``read()`` right after ``start()``
    sees real content instead of a misleading empty string for the common
    case.

    This is honest best-effort, not a guarantee: there is no tmux-native
    "the command has now printed something" event to wait on, only the
    pane's captured content itself, so this polls it. A command that
    takes longer than *budget* to produce any output (a slow-starting dev
    server, a large compile) will still read back empty immediately after
    ``start()`` returns -- exactly the pre-existing behavior -- because a
    longer, unconditional wait would either (a) still not be long enough
    for an arbitrarily slow command, or (b) make EVERY ``start()`` call
    pay a large fixed tax for the common fast case. Never raises: both
    ``observe.capture_pane()`` and ``observe.pane_is_dead()`` already
    degrade to a safe default (``""`` / ``False``) on any tmux error, per
    their own docstrings, so a transient failure here is simply treated as
    "not ready yet" and retried until *budget* elapses.
    """
    deadline = time.monotonic() + budget
    while True:
        if (await observe.capture_pane(name, 5)).strip():
            return
        if await observe.pane_is_dead(name):
            return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(interval)


async def start(
    name: str,
    command: str | None = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> tuple[bool, str | None]:
    """Create a new tmux session named *name*.

    *command*, if given, becomes the session's initial foreground command,
    passed to tmux as ONE shell-quoted argument (``shlex.quote()``) so it
    reaches tmux intact and is re-executed via tmux's OWN ``$SHELL -c`` --
    an unquoted multi-word command would otherwise be split by the
    wrapping shell this function's template itself runs through (e.g.
    ``"echo hi; sleep 300"`` would run ``sleep 300`` in the WRAPPER, not
    the session, blocking ``spawn_session()``'s own 30s call for no
    reason -- a real bug found while building this facade's own example).
    Omit *command* for a bare interactive shell. *cwd*, if given, is passed
    through tmux's own ``-c`` start-directory flag.

    *name* IS validated here (unlike ``spawn.spawn_session()``, whose own
    contract deliberately leaves that to the caller -- see its docstring,
    unchanged): rejected up front, before any tmux call, with
    ``ValueError`` if it fails ``names.is_valid_session_name()`` or would
    be silently mangled by tmux (``names.is_tmux_stable_name()`` -- tmux
    3.4 turns ``.`` into ``_`` at creation time with exit code 0 and no
    error). This mirrors ``rename()``'s exact guard below, so the two
    session-naming entry points this facade owns behave consistently: a
    caller who picks a mangle-prone name finds out immediately, at the
    call that would have created the mismatch, rather than getting back a
    session it can never look up again by the name it asked for.

    Before returning on a successful spawn, best-effort waits (bounded,
    see ``_wait_for_pane_ready()``'s docstring for exactly what this does
    and does not guarantee) for the new session's pane to show its first
    output, or for its command to have already exited -- whichever comes
    first -- so the obvious "start(), then immediately read()" usage this
    library's own quickstart demonstrates sees real content, not a
    misleadingly empty string, for the common case of a command that
    prints something quickly.

    Returns ``(ok, error)`` -- see ``spawn_session()``'s docstring for the
    exact success/failure semantics (including the exists-after-nonzero-
    exit TTY-attach tolerance).

    Raises:
        ValueError: *name* fails validation, before any tmux call is made
            (same failure class as ``rename()``'s ``new_name`` check).
    """
    # Validate BEFORE _ensure_wired(): an invalid/mangle-prone name must
    # fail with no side effect at all -- including not lazily installing
    # the default env-factory wiring, which would otherwise leak global
    # state (a real bug found writing this fix's own tests: a caller that
    # only ever sees the ValueError path still triggered _ensure_wired()'s
    # first-installed-factory side effect under the old ordering).
    if not names.is_valid_session_name(name):
        raise ValueError(f"{name!r} is not a valid session name")
    if not names.is_tmux_stable_name(name):
        raise ValueError(
            f"{name!r} contains '.', which tmux silently mangles to '_' "
            "at session-creation time -- choose a name without '.'"
        )
    _ensure_wired()
    parts = ["tmux", "new-session", "-d", "-s", "{name}"]
    if cwd is not None:
        parts += ["-c", shlex.quote(str(cwd))]
    if command:
        parts.append(shlex.quote(command))
    template = " ".join(parts)
    ok, err = await spawn.spawn_session(name, template)
    if ok:
        await _wait_for_pane_ready(name)
    return ok, err


async def stop(name: str) -> None:
    """Send Ctrl-C to *name*'s active pane -- a graceful stop request.

    The session and its pane are left running afterward; see
    ``lifecycle.interrupt_session()``'s docstring for exactly what this
    does and does not guarantee. Use ``kill()`` for an immediate, hard
    stop.
    """
    _ensure_wired()
    await lifecycle.interrupt_session(name)


async def kill(name: str) -> None:
    """Kill *name* outright (``tmux kill-session``) -- immediate, hard stop.

    See ``lifecycle.kill_session()``'s docstring for the exact targeting
    and error semantics.
    """
    _ensure_wired()
    await lifecycle.kill_session(name)


async def rename(old_name: str, new_name: str) -> str:
    """Rename *old_name* to *new_name*, verifying the result.

    Rejects *new_name* up front if it fails ``is_valid_session_name()`` or
    would be silently mangled by tmux (``is_tmux_stable_name()`` --
    tmux 3.4 turns ``.`` into ``_`` at rename time with exit code 0 and no
    error). After the rename call succeeds, re-enumerates and returns the
    OBSERVED name -- ``names.rename_tmux_session()``'s own docstring
    requires this verification step; this function performs it so callers
    don't have to remember to.

    Raises:
        ValueError: *new_name* fails validation before any tmux call is made.
        RuntimeError: tmux refused the rename (its own stderr, via
            ``run_tmux``), or reported success but *new_name* is not
            observed as live afterward.
    """
    _ensure_wired()
    if not names.is_valid_session_name(new_name):
        raise ValueError(f"{new_name!r} is not a valid session name")
    if not names.is_tmux_stable_name(new_name):
        raise ValueError(
            f"{new_name!r} contains '.', which tmux silently mangles to '_' "
            "at rename time -- choose a name without '.'"
        )
    await names.rename_tmux_session(old_name, new_name)
    observed = await observe.enumerate_sessions()
    if new_name not in observed:
        raise RuntimeError(
            f"tmux reported the rename succeeded, but {new_name!r} is not "
            f"present after re-enumeration: {observed!r}"
        )
    return new_name


# ---------------------------------------------------------------------------
# Observation: list / status / read / page / search
# ---------------------------------------------------------------------------


async def list_sessions() -> list[SessionInfo]:
    """Enumerate all sessions visible on the configured socket, with
    running/activity/created/cwd for each.

    ``running`` is ``not pane_is_dead(name)`` -- see
    ``observe.pane_is_dead()``'s docstring for the "finished vs still
    going" distinction this answers. Dead-pane checks for every session run
    concurrently (``asyncio.gather``), matching ``observe.snapshot_all()``'s
    own concurrency convention; a check that raises is treated as "not
    observed as dead" (unknown, not dead -- the same convention
    ``pane_is_dead()`` itself applies to errors).
    """
    _ensure_wired()
    session_names = await observe.enumerate_sessions()
    activity = observe.get_session_activity()
    created = observe.get_session_created_times()
    cwds = observe.get_session_cwds()
    dead_flags = await asyncio.gather(
        *(observe.pane_is_dead(n) for n in session_names), return_exceptions=True
    )
    infos: list[SessionInfo] = []
    for n, dead in zip(session_names, dead_flags):
        is_dead = dead is True  # BaseException -> not observed as dead
        infos.append(
            SessionInfo(
                name=n,
                running=not is_dead,
                activity=activity.get(n),
                created=created.get(n),
                cwd=cwds.get(n),
            )
        )
    return infos


async def status(name: str) -> Literal["missing", "running", "finished"]:
    """Answer "is it done, or still going?" for one session.

    Returns:
        ``"missing"``  -- no session by this name exists right now.
        ``"finished"`` -- the session exists but its active pane's
                          foreground command has exited
                          (``observe.pane_is_dead()``).
        ``"running"``  -- the session exists and its pane is not dead.
    """
    _ensure_wired()
    session_names = await observe.enumerate_sessions()
    if name not in session_names:
        return "missing"
    if await observe.pane_is_dead(name):
        return "finished"
    return "running"


async def is_running(name: str) -> bool:
    """True iff ``status(name) == "running"``. A convenience for the
    common case that doesn't need to distinguish "missing" from
    "finished"."""
    return await status(name) == "running"


async def read(name: str, lines: int = observe.DEFAULT_CAPTURE_LINES) -> str:
    """Capture the last *lines* lines of *name*'s pane -- a thin,
    wired-by-default wrapper over ``observe.capture_pane()``."""
    _ensure_wired()
    return await observe.capture_pane(name, lines)


async def page(name: str, *, start: int | None = None, count: int = 100) -> PageResult:
    """Read a page of *name*'s scrollback by ABSOLUTE line number, doing
    the two-call conversion ``observe``'s module docstring describes so a
    caller never has to learn tmux's own relative ``-S``/``-E`` coordinate
    system.

    Absolute line 0 is the oldest line currently in the history buffer;
    line numbers increase toward the present. ``start=None`` (the default)
    returns the most recent *count* lines (identical to ``read()``, but
    carrying the same ``PageResult`` shape as an explicit page for a
    uniform caller). ``start=N`` returns up to *count* lines beginning at
    absolute line *N*.

    Returns a ``PageResult`` whose ``total`` and ``saturated`` are computed
    from the SAME tmux call that produced ``text`` (the paired-read
    convention ``observe.capture_pane_window()``'s docstring describes),
    so they are always truthful for what was actually captured.
    """
    _ensure_wired()
    history_size, _pane_height, _history_limit = await observe.capture_pane_metadata(
        name
    )
    if start is None:
        s, e = -count, None
    else:
        rel_start = start - history_size
        s, e = rel_start, rel_start + count - 1
    h2, p2, l2, text = await observe.capture_pane_window(name, s, e)
    lines = text.splitlines()
    resolved_start = start if start is not None else max(0, h2 - count)
    return PageResult(
        text=text,
        start=resolved_start,
        total=h2 + p2,
        returned=len(lines),
        saturated=l2 > 0 and h2 >= l2,
    )


# Default cap on how much scrollback search() will pull from tmux in one
# call -- generous enough to cover most sessions' full history while
# bounding the cost of a search against a session with an enormous or
# unlimited history-limit. Callers with a genuinely deeper search need
# raise max_lines explicitly.
DEFAULT_SEARCH_MAX_LINES = 5000
DEFAULT_SEARCH_MAX_MATCHES = 50


async def search(
    name: str,
    pattern: str,
    *,
    regex: bool = False,
    max_lines: int = DEFAULT_SEARCH_MAX_LINES,
    max_matches: int = DEFAULT_SEARCH_MAX_MATCHES,
) -> SearchResult:
    """Search *name*'s scrollback for *pattern* ("did it print an error?").

    Pulls up to *max_lines* of the most recent history in ONE
    ``capture_pane_window()`` call (via ``page()``'s own history-size
    probe), then searches client-side -- a plain substring check by
    default, or ``re.search`` per line when ``regex=True``. Returns at most
    *max_matches* matches; ``truncated`` is True when either the scrollback
    itself exceeds *max_lines* (not everything was searched) or the match
    cap was hit (not everything found was returned) -- either way, a
    caller must not read an untruncated result as "no more matches exist"
    without checking this flag.
    """
    _ensure_wired()
    history_size, pane_height, _history_limit = await observe.capture_pane_metadata(
        name
    )
    available = history_size + pane_height
    window = min(available, max_lines)
    _h2, _p2, _l2, text = await observe.capture_pane_window(name, -window, None)
    lines = text.splitlines()
    start_abs = max(0, available - len(lines))
    truncated = available > window

    matcher = re.compile(pattern) if regex else None
    matches: list[SearchMatch] = []
    for i, line in enumerate(lines):
        hit = matcher.search(line) is not None if matcher else (pattern in line)
        if hit:
            matches.append(SearchMatch(line=start_abs + i, text=line))
            if len(matches) >= max_matches:
                truncated = True
                break
    return SearchResult(matches=matches, truncated=truncated)


# ---------------------------------------------------------------------------
# Attention: wait_for_attention
# ---------------------------------------------------------------------------


async def wait_for_attention(
    name: str,
    *,
    timeout: float | None = None,
    poll_interval: float = DEFAULT_BELL_POLL_INTERVAL,
) -> bool:
    """Block until *name* rings its bell (e.g. a program stopping to ask
    ``[y/n]``), or *timeout* seconds pass.

    Thin wrapper over ``bell.wait_for_bell()`` -- see its docstring for the
    exact polling contract and return semantics.
    """
    _ensure_wired()
    return await wait_for_bell(name, timeout=timeout, poll_interval=poll_interval)


# ---------------------------------------------------------------------------
# Preflight: doctor
# ---------------------------------------------------------------------------


async def doctor() -> DoctorReport:
    """ "Will this work here?" -- a one-call preflight, composing facts that
    already exist in this package but were previously undiscoverable
    without reading source: tmux's presence/version, ``cgroup.environment_mode()``
    (which already answers "does the cgroup-escape hazard even apply here" --
    e.g. it returns ``"not-applicable"`` on macOS and in most containers,
    the single biggest unanswered adoption question this library had), the
    real (cached) ``cgroup.should_escape()`` capability probe when a scope
    looks possible, and whether the facade's configured socket directory is
    actually writable.

    Never raises for an environment problem -- every check degrades to a
    boolean/None plus a human-readable line in ``notes``, so a consumer can
    show this report to a user (or an agent) without a try/except.
    """
    notes: list[str] = []

    tmux_path = shutil.which("tmux")
    tmux_version: str | None = None
    if tmux_path:
        try:
            proc_handle = await asyncio.create_subprocess_exec(
                "tmux",
                "-V",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await proc_handle.communicate()
            tmux_version = out.decode("utf-8", errors="replace").strip()
        except OSError as exc:
            notes.append(f"tmux found at {tmux_path} but `tmux -V` failed: {exc}")
    else:
        notes.append("tmux not found on PATH -- nothing in this library will work")

    mode = cgroup.environment_mode()
    escape_ready = False
    if mode == "scope-candidate":
        escape_ready = await cgroup.should_escape()
        if not escape_ready:
            notes.append(
                "a systemd --user session looked available but the "
                "cgroup-escape self-test failed -- tmux servers this "
                "process spawns are NOT protected from a future service "
                "restart (see tmux_kit/cgroup.py's module docstring)"
            )
    else:
        notes.append(
            "cgroup_mode is 'not-applicable' -- either not Linux, or no "
            "usable systemd --user session (common in containers): the "
            "44-session cgroup-restart hazard this library guards against "
            "does not apply here, and nothing needs to be done about it"
        )

    socket_dir = default_socket_dir()
    socket_writable = True
    try:
        socket_dir.mkdir(parents=True, exist_ok=True)
        probe = socket_dir / ".tmux-kit-doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        socket_writable = False
        notes.append(f"default socket dir {socket_dir} is not writable: {exc}")

    return DoctorReport(
        tmux_found=bool(tmux_path),
        tmux_version=tmux_version,
        cgroup_mode=mode,
        cgroup_escape_ready=escape_ready,
        socket_dir=str(socket_dir),
        socket_dir_writable=socket_writable,
        notes=notes,
    )
