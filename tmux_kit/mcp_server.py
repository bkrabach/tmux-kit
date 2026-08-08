"""tmux-kit MCP server -- the exact ``tmux_kit.api`` verbs, as MCP tools.

Optional extra (``pip install 'tmux-kit[mcp]'``) -- the base ``tmux-kit``
package stays stdlib-only; this module is the only place in the package
permitted to import the ``mcp`` SDK (see ``tests/test_rails.py``'s
MCP-scoped import rail). If ``mcp`` isn't installed, importing this module
fails with a clear, actionable message instead of a bare
``ModuleNotFoundError`` traceback.

Every tool's docstring carries the same agent-grade context as the CLI's
``--help`` (when to reach for it, what it returns, what it raises) --
that's what FastMCP surfaces to a calling agent as the tool description.
One vocabulary, one data shape, three doors in (library, CLI, MCP server).

Run as a stdio server:

    python -m tmux_kit.mcp_server
    # or, if installed: tmux-kit-mcp
"""

from __future__ import annotations

import dataclasses
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "tmux-kit MCP server requires the 'mcp' extra: pip install 'tmux-kit[mcp]'",
        file=sys.stderr,
    )
    sys.exit(1)

from tmux_kit import api

mcp = FastMCP("tmux-kit")


@mcp.tool()
async def start(name: str, command: str | None = None, cwd: str | None = None) -> dict:
    """Create a new, detached tmux session.

    WHEN TO USE: you need a new named tmux session running a command (a
    dev server, a build, a long task) that keeps running after this call
    returns.

    ARGS:
        name: session name (not validated by this tool -- pick something
            unique; a name that collides with a live session fails).
        command: initial foreground command. Passed to tmux as ONE
            shell-quoted argument, so ';'/'&&' inside it run INSIDE the
            session, not in a wrapping shell. Omit for a bare shell.
        cwd: start directory (tmux's own -c flag).

    RETURNS: {"ok": bool, "error": str | null}. ``ok`` is False (not an
    exception) for an ordinary failure like "command not on PATH" --
    check it explicitly.
    """
    ok, err = await api.start(name, command, cwd=cwd)
    return {"ok": ok, "error": err}


@mcp.tool()
async def list_sessions() -> list[dict]:
    """List every session visible on the configured socket.

    WHEN TO USE: "what's running right now?" -- usually the first tool
    call before using any other tool below, to discover session names.

    RETURNS: a list of {"name", "running", "activity", "created", "cwd"}.
    ``running`` is False when the session's active pane's command has
    already exited (same "is it done, or still going?" distinction as
    ``status``, computed per-session here).
    """
    sessions = await api.list_sessions()
    return [dataclasses.asdict(s) for s in sessions]


@mcp.tool()
async def status(name: str) -> str:
    """Answer "is it done, or still going?" for one session.

    WHEN TO USE: you started a long job and need to know -- without
    reading its output -- whether it's still running, finished (its
    pane's command exited), or gone entirely.

    RETURNS: one of "missing", "running", "finished".
    """
    return await api.status(name)


@mcp.tool()
async def read(name: str, lines: int = 30) -> str:
    """Capture the last N lines of a session's pane ("what did it just print?").

    For history deeper than this recent window, use ``page``. To find one
    thing across a lot of history, use ``search``.

    RETURNS: the captured text, ANSI escapes included. Empty string if the
    session is missing or unreachable (never raises for that).
    """
    return await api.read(name, lines)


@mcp.tool()
async def page(name: str, start: int | None = None, count: int = 100) -> dict:
    """Read one page of scrollback by ABSOLUTE line number.

    WHEN TO USE: reading history deeper than ``read``'s recent-lines
    window, one page at a time. Absolute line 0 is the OLDEST line
    currently in the history buffer; ``start=None`` (the default) returns
    the most recent ``count`` lines.

    RETURNS: {"text", "start", "total", "returned", "saturated"} -- see
    ``tmux_kit.api.page()``'s docstring for exactly what each field means.
    """
    result = await api.page(name, start=start, count=count)
    return dataclasses.asdict(result)


@mcp.tool()
async def search(
    name: str,
    pattern: str,
    regex: bool = False,
    max_lines: int = api.DEFAULT_SEARCH_MAX_LINES,
    max_matches: int = api.DEFAULT_SEARCH_MAX_MATCHES,
) -> dict:
    """Search a session's scrollback for `pattern` ("did it print an error?").

    ARGS:
        regex: treat `pattern` as a regular expression (per-line
            `re.search`) instead of a plain substring.
        max_lines / max_matches: caps on how much history to pull and how
            many matches to return -- raise them for a deeper/wider search.

    RETURNS: {"matches": [{"line", "text"}, ...], "truncated": bool}.
    ``truncated=True`` means not everything was searched or not every
    match was returned -- never treat a result as exhaustive without
    checking this flag.
    """
    result = await api.search(
        name, pattern, regex=regex, max_lines=max_lines, max_matches=max_matches
    )
    return dataclasses.asdict(result)


@mcp.tool()
async def wait_for_attention(name: str, timeout: float | None = None) -> bool:
    """Block until a session rings its bell (e.g. it stopped to ask [y/n]).

    WHEN TO USE: waiting for a program that pauses for interactive input,
    instead of repeatedly calling ``read`` in a loop.

    RETURNS: True as soon as the bell rings. False if `timeout` seconds
    elapse first (omit `timeout`, the default, to wait forever -- bound
    this call yourself if you need an outer limit).
    """
    return await api.wait_for_attention(name, timeout=timeout)


@mcp.tool()
async def stop(name: str) -> str:
    """Send Ctrl-C to a session's active pane -- a graceful stop request.

    The session and its pane are left running afterward -- call `status`
    afterward to see whether the command actually stopped. For an
    immediate, unconditional stop of the whole session, use `kill`.

    RETURNS: a short confirmation string. Raises if the session doesn't
    exist or tmux is unreachable.
    """
    await api.stop(name)
    return f"sent Ctrl-C to {name!r}"


@mcp.tool()
async def kill(name: str) -> str:
    """Kill a session outright (tmux kill-session) -- immediate, hard stop.

    Use `stop` instead if you want to ask the running command to exit
    gracefully while keeping the session around.

    RETURNS: a short confirmation string. Raises if the session doesn't
    exist or tmux is unreachable.
    """
    await api.kill(name)
    return f"killed {name!r}"


@mcp.tool()
async def rename(old_name: str, new_name: str) -> str:
    """Rename a session, verifying tmux didn't silently mangle the result.

    Rejects `new_name` up front if it's invalid or contains '.' (tmux 3.4
    silently turns '.' into '_' at rename time with no error) -- raising
    rather than returning a name that isn't what was asked for.

    RETURNS: the OBSERVED new name on success (always equal to
    `new_name`, since a mangle-prone name is rejected before any tmux
    call). Raises on failure, with the reason in the message.
    """
    return await api.rename(old_name, new_name)


@mcp.tool()
async def doctor() -> dict:
    """ "Will this work here?" -- a one-call environment preflight.

    WHEN TO USE: before relying on tmux-kit in a new environment
    (container, CI runner, fresh host) -- answers "is tmux installed?",
    "does the systemd cgroup-escape hazard even apply here?" (it doesn't
    on macOS or in most containers), and "is the socket directory
    writable?" in one call.

    RETURNS: {"tmux_found", "tmux_version", "cgroup_mode",
    "cgroup_escape_ready", "socket_dir", "socket_dir_writable", "notes"}.
    Never raises for an environment problem -- read the fields/notes.
    """
    return dataclasses.asdict(await api.doctor())


def main() -> None:
    """Entry point: run the stdio MCP server (blocks until the client
    disconnects)."""
    mcp.run()


if __name__ == "__main__":
    main()
