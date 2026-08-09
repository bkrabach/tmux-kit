"""tmux-kit CLI -- the exact ``tmux_kit.api`` verbs, as a Click command group.

Optional extra (``pip install 'tmux-kit[cli]'``) -- the base ``tmux-kit``
package stays stdlib-only; this module is the only place in the package
permitted to import ``click`` (see ``tests/test_rails.py``'s CLI-scoped
import rail). If ``click`` isn't installed, importing this module (and
therefore running the ``tmux-kit`` console script) fails with a clear,
actionable message instead of a bare ``ModuleNotFoundError`` traceback.

Every command's ``--help`` is written to be read COLD by an agent with no
other context: what it does, when to reach for it, what it returns, what
fails and why, and its exit codes. ``--json`` is available on every
read-oriented command, emitting the exact same ``tmux_kit.api`` dataclass
shapes an ``import tmux_kit`` caller would get back -- one vocabulary, one
data shape, three doors in (library, CLI, MCP).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from typing import Any

try:
    import click
except ImportError:
    print(
        "tmux-kit CLI requires the 'cli' extra: pip install 'tmux-kit[cli]'",
        file=sys.stderr,
    )
    sys.exit(1)

from tmux_kit import api
from tmux_kit.observe import DEFAULT_CAPTURE_LINES


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(_to_jsonable(value), indent=2, default=str))
    else:
        click.echo(value)


def _json_option() -> Any:
    return click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        help="Emit machine-readable JSON (the same shape tmux_kit.api returns) instead of a human-readable line.",
    )


@click.group()
def main() -> None:
    """tmux-kit: create, observe, and control tmux sessions from the shell.

    Every subcommand below wraps the exact same function in
    ``tmux_kit.api`` -- nothing here does anything an ``import tmux_kit``
    caller couldn't also do directly. Session NAMEs are not validated by
    this CLI (same contract as the library): pass a name you control, or
    one that came back from `tmux-kit list`.

    SCOPE: every command here talks ONLY to tmux-kit's own configured
    socket (see `tmux-kit doctor`'s `socket_dir`) -- a DIFFERENT,
    deliberately isolated tmux server from any ambient session (e.g. the
    terminal you are typing this command into, if it happens to be a
    tmux pane). `tmux-kit list` returning nothing means "nothing on
    tmux-kit's own socket," NOT "nothing running on this host" -- if that
    contradicts what you expect, do NOT "double check" with a bare `tmux`
    command. If you (or an agent driving this CLI) are inside a tmux pane,
    `$TMUX` is set and a bare `tmux` invocation resolves against THAT
    ambient server in preference to any `TMUX_TMPDIR` override -- this is
    the exact mechanism that destroyed 73 of an operator's live sessions
    in a real incident (see this repo's AGENTS.md, "`TMUX_TMPDIR` is not
    an isolation boundary"). Need to run a real, throwaway tmux command to
    investigate instead? Use `tmux_kit.isolation.isolated_tmux_server()`
    (a Python context manager -- there is no CLI equivalent), never a bare
    `tmux` command.
    """


@main.command()
@click.argument("name")
@click.option(
    "--command",
    default=None,
    help=(
        "Initial foreground command for the session (default: a bare "
        "interactive shell). Passed to tmux as ONE shell-quoted argument, "
        "so ';'/'&&' inside it run INSIDE the session, not in the "
        "wrapping shell."
    ),
)
@click.option(
    "--cwd", default=None, help="Start directory for the session (tmux's own -c flag)."
)
def start(name: str, command: str | None, cwd: str | None) -> None:
    """Create a new, detached tmux session.

    WHEN TO USE: you need a new named tmux session running a command (a
    dev server, a build, a long agent task) that keeps running after this
    CLI invocation exits.

    SCOPE: created on tmux-kit's own socket only (see `tmux-kit --help`'s
    SCOPE section) -- not visible to a human's ambient `tmux attach`
    unless they know to point at this same socket.

    RETURNS: "started 'NAME'" on success.

    FAILS WHEN: NAME is invalid, or contains '.' (tmux 3.4 silently
    mangles '.' to '_' at creation time with no error -- rejected here
    up front instead), the command isn't on PATH, or tmux otherwise
    refuses (its stderr is printed). Does NOT fail just because the
    session's initial command later exits -- that's `tmux-kit status
    NAME` -> "finished".

    EXIT CODES: 0 success, 1 failure (reason on stderr).
    """
    try:
        ok, err = _run(api.start(name, command, cwd=cwd))
    except ValueError as exc:
        click.echo(f"failed to start {name!r}: {exc}", err=True)
        raise SystemExit(1) from exc
    if not ok:
        click.echo(f"failed to start {name!r}: {err}", err=True)
        raise SystemExit(1)
    click.echo(f"started {name!r}")


@main.command("list")
@_json_option()
def list_cmd(as_json: bool) -> None:
    """List every session visible on the configured socket.

    WHEN TO USE: "what's running right now?" -- usually the first command
    run to discover session names before using any other command below.

    SCOPE: tmux-kit's own socket only (see `tmux-kit --help`'s SCOPE
    section) -- "(no sessions)" means none on THIS socket, not "nothing
    running on this host." Never fall back to a bare `tmux` command to
    check further.

    RETURNS (human): one line per session -- name, tab, "running" or
    "finished" (see `status --help` for that distinction).
    RETURNS (--json): the full list of tmux_kit.api.SessionInfo (name,
    running, activity, created, cwd).
    """
    sessions = _run(api.list_sessions())
    if as_json:
        _emit(sessions, as_json=True)
        return
    if not sessions:
        click.echo("(no sessions)")
        return
    for s in sessions:
        click.echo(f"{s.name}\t{'running' if s.running else 'finished'}")


@main.command()
@click.argument("name")
@_json_option()
def status(name: str, as_json: bool) -> None:
    """Answer "is it done, or still going?" for one session.

    WHEN TO USE: you started a long job and need to know -- without
    attaching or reading output -- whether it's still running, finished
    (its pane's foreground command exited), or gone entirely.

    SCOPE: checked against tmux-kit's own socket only (see `tmux-kit
    --help`'s SCOPE section) -- "missing" means "not on tmux-kit's
    socket," not "not running anywhere." Never fall back to a bare `tmux`
    command to check further.

    RETURNS: one of "missing", "running", "finished" (see
    tmux_kit.api.status()'s docstring for the exact rule).

    EXIT CODES: always 0 -- "missing" is a valid, successfully-determined
    answer, not a CLI failure.
    """
    _emit(_run(api.status(name)), as_json=as_json)


@main.command("exit-code")
@click.argument("name")
@_json_option()
def exit_code_cmd(name: str, as_json: bool) -> None:
    """Answer "did it SUCCEED?" for a finished session -- `status` doesn't.

    WHEN TO USE: `status NAME` already reported "finished" and you need to
    know whether the command actually succeeded (exit 0) or failed
    (nonzero), not just that it stopped -- "did the build pass?", not
    just "is the build done?".

    SCOPE: tmux-kit's own socket only (see `tmux-kit --help`'s SCOPE
    section).

    CAVEAT: tmux only remembers a pane's exit status if that session/
    window has `remain-on-exit on` set -- by tmux's factory default
    (`remain-on-exit off`), a finished pane (and the session with it, if
    it was the last pane) is torn down immediately, so there is often
    nothing left to ask. `null`/empty output here does NOT mean "it
    succeeded" -- it means "not knowable" (still running, session gone,
    or exit status not retained). Check `status NAME` first.

    RETURNS: the integer exit code (0 typically success, nonzero
    failure), or nothing (empty line / JSON `null`) if not knowable.
    """
    result = _run(api.exit_code(name))
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo("" if result is None else str(result))


@main.command()
@click.argument("name")
@click.option(
    "--lines",
    default=DEFAULT_CAPTURE_LINES,
    show_default=True,
    help="How many of the most recent lines to capture.",
)
def read(name: str, lines: int) -> None:
    """Capture the last N lines of a session's pane.

    WHEN TO USE: "what did it just print?" -- a quick look at recent
    output without paging through scrollback. For deeper history use
    `tmux-kit page`; to search for one thing across a lot of history use
    `tmux-kit search`.

    SCOPE: tmux-kit's own socket only (see `tmux-kit --help`'s SCOPE
    section). Never fall back to a bare `tmux` command to look further.

    RETURNS: the captured text, printed as-is (ANSI escapes included, same
    as `tmux capture-pane -e`).

    FAILS WHEN: it doesn't -- an unreachable/missing session returns ''
    (empty output), matching tmux_kit.observe.capture_pane()'s contract.
    """
    click.echo(_run(api.read(name, lines)), nl=False)


@main.command()
@click.argument("name")
@click.option(
    "--start",
    "start_line",
    type=int,
    default=None,
    help="Absolute scrollback line to begin at (0 = oldest available line). Omit for the most recent --count lines.",
)
@click.option(
    "--count", default=100, show_default=True, help="How many lines to return."
)
@_json_option()
def page(name: str, start_line: int | None, count: int, as_json: bool) -> None:
    """Read one page of scrollback by ABSOLUTE line number.

    WHEN TO USE: reading history deeper than `read`'s recent-lines window,
    one page at a time, without learning tmux's own relative -S/-E
    coordinate system (this command does that conversion for you -- see
    tmux_kit.api.page()'s docstring).

    SCOPE: tmux-kit's own socket only (see `tmux-kit --help`'s SCOPE
    section). Never fall back to a bare `tmux` command to look further.

    RETURNS (--json): tmux_kit.api.PageResult (text, start, total,
    returned, saturated). RETURNS (human): the text, then a one-line
    summary of start/total/saturated on stderr so it doesn't pollute
    captured stdout.
    """
    result = _run(api.page(name, start=start_line, count=count))
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo(result.text, nl=False)
    click.echo(
        f"[start={result.start} returned={result.returned} total={result.total} saturated={result.saturated}]",
        err=True,
    )


@main.command()
@click.argument("name")
@click.argument("pattern")
@click.option(
    "--regex",
    is_flag=True,
    default=False,
    help="Treat PATTERN as a regular expression (re.search per line) instead of a plain substring.",
)
@click.option(
    "--max-lines",
    default=api.DEFAULT_SEARCH_MAX_LINES,
    show_default=True,
    help="Cap on how much scrollback to pull before searching.",
)
@click.option(
    "--max-matches",
    default=api.DEFAULT_SEARCH_MAX_MATCHES,
    show_default=True,
    help="Cap on how many matches to return.",
)
@_json_option()
def search(
    name: str,
    pattern: str,
    regex: bool,
    max_lines: int,
    max_matches: int,
    as_json: bool,
) -> None:
    """Search a session's scrollback for PATTERN ("did it print an error?").

    WHEN TO USE: you don't know WHERE in a session's history something
    appeared -- an error, a prompt, a specific log line -- just that you
    need to find it.

    SCOPE: tmux-kit's own socket only (see `tmux-kit --help`'s SCOPE
    section). Never fall back to a bare `tmux` command to search further.

    RETURNS (--json): tmux_kit.api.SearchResult (matches: [{line, text}],
    truncated). RETURNS (human): one "LINE: TEXT" per match; a trailing
    "(truncated)" note on stderr if not everything was searched/returned.
    """
    result = _run(
        api.search(
            name, pattern, regex=regex, max_lines=max_lines, max_matches=max_matches
        )
    )
    if as_json:
        _emit(result, as_json=True)
        return
    for m in result.matches:
        click.echo(f"{m.line}: {m.text}")
    if result.truncated:
        click.echo("(truncated -- not all scrollback/matches were returned)", err=True)


@main.command()
@click.argument("name")
@click.option(
    "--timeout",
    type=float,
    default=None,
    help=(
        "Give up after this many seconds (default: wait forever -- an "
        "explicit choice for an interactive CLI invocation with a human "
        "who can Ctrl-C; contrast the MCP `wait_for_attention` tool, "
        "which defaults to a finite 30s for exactly the opposite reason)."
    ),
)
def wait(name: str, timeout: float | None) -> None:
    """Block until a session rings its bell (e.g. it stopped to ask [y/n]).

    WHEN TO USE: an agent or script that needs to know the MOMENT a
    program wants attention, instead of polling `tmux-kit read` in a loop.

    SCOPE: tmux-kit's own socket only (see `tmux-kit --help`'s SCOPE
    section).

    STICKY FLAG WARNING: the underlying tmux bell flag is NOT cleared by
    reading it. If a bell already rang and nothing has cleared the flag
    since, this returns "bell" immediately every time it's called for
    that session, even for a bell that already happened -- do not treat a
    repeated "bell" as proof of a NEW event without checking `read` too.

    RETURNS: "bell" and exit code 0 as soon as the bell rings (or
    immediately, if it already had -- see the sticky-flag warning above).
    If --timeout elapses first: "timeout" and exit code 1 -- read that as
    "still working, ask again", not as an error.
    """
    rang = _run(api.wait_for_attention(name, timeout=timeout))
    if rang:
        click.echo("bell")
        return
    click.echo("timeout", err=True)
    raise SystemExit(1)


@main.command()
@click.argument("name")
def stop(name: str) -> None:
    """Send Ctrl-C to a session's active pane -- a graceful stop request.

    WHEN TO USE: you want the RUNNING COMMAND to stop, but want the
    session itself to remain (e.g. to inspect the resulting shell prompt
    or its exit output). For an immediate, unconditional stop of the
    whole session use `tmux-kit kill`.

    SCOPE: NAME is resolved against tmux-kit's own socket only (see
    `tmux-kit --help`'s SCOPE section).

    RETURNS: "sent Ctrl-C to 'NAME'". Does not confirm the command
    actually stopped -- check with `tmux-kit status NAME` afterward.
    """
    _run(api.stop(name))
    click.echo(f"sent Ctrl-C to {name!r}")


@main.command()
@click.argument("name")
def kill(name: str) -> None:
    """Kill a session outright (tmux kill-session) -- immediate, hard stop.

    WHEN TO USE: you're done with a session and want it gone now,
    regardless of what's running in it. For a graceful "ask it to stop
    first" use `tmux-kit stop`.

    SCOPE: NAME is resolved against tmux-kit's own socket only (see
    `tmux-kit --help`'s SCOPE section).

    FAILS WHEN: NAME doesn't exist, or tmux is unreachable (its stderr is
    printed). EXIT CODES: 0 success, 1 failure.
    """
    try:
        _run(api.kill(name))
    except RuntimeError as exc:
        click.echo(f"failed to kill {name!r}: {exc}", err=True)
        raise SystemExit(1) from exc
    click.echo(f"killed {name!r}")


@main.command()
@click.argument("old_name")
@click.argument("new_name")
def rename(old_name: str, new_name: str) -> None:
    """Rename a session, verifying tmux didn't silently mangle the result.

    WHEN TO USE: renaming a session where the new name might collide with
    tmux's own '.'-mangling rule (tmux 3.4 silently turns '.' into '_').

    RETURNS: the OBSERVED new name on success (identical to NEW_NAME,
    since this command rejects any name tmux would mangle up front).

    SCOPE: resolved against tmux-kit's own socket only (see `tmux-kit
    --help`'s SCOPE section).

    FAILS WHEN: NEW_NAME is invalid, contains '.', or tmux refuses (e.g.
    duplicate session) -- reason printed, exit code 1.
    """
    try:
        observed = _run(api.rename(old_name, new_name))
    except (ValueError, RuntimeError) as exc:
        click.echo(f"failed to rename {old_name!r} -> {new_name!r}: {exc}", err=True)
        raise SystemExit(1) from exc
    click.echo(observed)


@main.command()
@_json_option()
def doctor(as_json: bool) -> None:
    """ "Will this work here?" -- a one-call environment preflight.

    WHEN TO USE: before relying on tmux-kit in a new environment
    (container, CI runner, a fresh host) -- answers "is tmux installed?",
    "does the systemd cgroup-escape hazard even apply here?" (it doesn't
    on macOS or in most containers -- the single biggest unanswered
    adoption question this library had), and "is the socket directory
    writable?" in one call.

    RETURNS (--json): tmux_kit.api.DoctorReport. RETURNS (human): each
    field on its own line, followed by any notes.

    EXIT CODES: always 0 -- this is a report, not a pass/fail gate; read
    `tmux_found`/`socket_dir_writable` yourself to decide if something's
    wrong.
    """
    report = _run(api.doctor())
    if as_json:
        _emit(report, as_json=True)
        return
    click.echo(f"tmux_found: {report.tmux_found}")
    click.echo(f"tmux_version: {report.tmux_version}")
    click.echo(f"cgroup_mode: {report.cgroup_mode}")
    click.echo(f"cgroup_escape_ready: {report.cgroup_escape_ready}")
    click.echo(f"socket_dir: {report.socket_dir}")
    click.echo(f"socket_dir_writable: {report.socket_dir_writable}")
    for note in report.notes:
        click.echo(f"note: {note}")


if __name__ == "__main__":
    main()
