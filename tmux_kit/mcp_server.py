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

AUTHORIZATION (0.3.0) -- destructive verbs are deny-by-default
================================================================
``stop`` (Ctrl-C, recoverable) and ``kill`` (kill-session, unrecoverable)
are gated by a policy the OPERATOR who launches this server process
supplies via environment variables -- never by the calling agent, and
never granted by any parameter on the tool call itself:

    TMUX_KIT_MCP_STOP_ENABLED=true
    TMUX_KIT_MCP_STOP_ALLOW=demo-*,scratch-*      # comma-separated globs
    TMUX_KIT_MCP_KILL_ENABLED=true
    TMUX_KIT_MCP_KILL_ALLOW=demo-*

Both tiers default to fully disabled: an unset, misspelled, or
non-``"true"``/``"1"``/``"yes"`` ``_ENABLED`` value refuses EVERY call for
that verb with ``PermissionError``, regardless of ``_ALLOW``. `stop` and
`kill` are independently configurable (separate env var pairs) so an
operator can permit a wider blast radius for the recoverable verb than the
unrecoverable one -- see ``tmux_kit.keys.destructive_action_allowed()``
for the exact matching semantics (case-insensitive glob, fail-closed).

Why this exists: an agent given raw tmux access -- this MCP server's exact
threat model -- hand-rolling a destructive tmux command is precisely the
incident class recorded in this project's AGENTS.md ("TMUX_TMPDIR is not
an isolation boundary": 73 real, live tmux sessions destroyed by exactly
this class of caller believing an isolation mechanism protected it when it
did not). Before this fence, `stop`/`kill` below called
``tmux_kit.api.stop()``/``kill()`` directly with NO check at all: any
session name reachable via `list_sessions` could be stopped or killed by
any MCP client connected to this process.

WHAT THIS FENCE DOES NOT COVER -- read this before assuming it is total:

- It gates ONLY the two MCP tools below (`stop`, `kill`). Calling
  ``tmux_kit.api.kill()`` / ``tmux_kit.lifecycle.kill_session()`` directly
  (as a library), or ``tmux-kit kill`` (the CLI extra), remains exactly as
  unguarded as before this change -- see ``tmux_kit/lifecycle.py``'s
  module docstring, unchanged. A human typing a CLI command already holds
  the same OS-level authority to run `tmux kill-session` directly; the MCP
  surface is the one that hands a *program* that authority with no human
  reviewing each individual call, which is why the fence lives here and
  nowhere else in this repo.
- It is ONE GLOBAL policy for the whole server PROCESS, not scoped per
  connected MCP client -- if several MCP clients share one running
  `tmux-kit-mcp` process, they share its one policy (the same "one global
  slot, last writer wins" caution ``tmux_kit/CONSUMERS.md`` documents for
  tmux's `alert-bell` hook).
- Its strength is exactly the operator's glob choice: an allowlist of
  ``"*"`` authorizes every session name and provides no protection at all.
  This fence can express "which session NAMES", nothing about caller
  identity, intent, or rate.
- It is NOT the deferred ``Sender``/``SendPolicy`` typed authorization
  object ``tmux_kit/CONSUMERS.md``'s "NOT in the library yet" section
  still holds open for a second real consumer to shape -- this is a
  narrower, MCP-scoped allowlist fence built from the existing
  ``tmux_kit.keys`` permission-fence primitive, not a replacement for that
  larger, still-unbuilt policy layer.
"""

from __future__ import annotations

import dataclasses
import os
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "tmux-kit MCP server requires the 'mcp' extra: pip install 'tmux-kit[mcp]'",
        file=sys.stderr,
    )
    sys.exit(1)

from tmux_kit import api, keys

mcp = FastMCP("tmux-kit")

# ---------------------------------------------------------------------------
# Deny-by-default authorization fence for the destructive lifecycle verbs
# (`stop`, `kill`) -- see the module docstring above for the full rationale
# and its honest coverage boundary.
# ---------------------------------------------------------------------------

_STOP_ENV_PREFIX = "TMUX_KIT_MCP_STOP"
_KILL_ENV_PREFIX = "TMUX_KIT_MCP_KILL"

# Env-var values (case/whitespace-insensitive) that count as "enabled".
# Anything else -- unset, "false", "0", a typo -- is disabled. There is no
# value that means "enabled" by accident.
_ENABLED_TRUE_VALUES = frozenset({"1", "true", "yes"})


def _policy_from_env(prefix: str) -> dict:
    """Build a fail-closed ``{"enabled", "allow"}`` policy dict from the
    ``{prefix}_ENABLED`` / ``{prefix}_ALLOW`` environment variables.

    Read FRESH on every call (never cached at import/startup time) so a
    test, or a supervising process that rewrites this server's own
    environment, sees a policy change take effect on the very next tool
    call -- no server restart required. ``{prefix}_ALLOW`` is a
    comma-separated list of glob patterns; empty/whitespace-only entries
    are dropped.
    """
    enabled = (
        os.environ.get(f"{prefix}_ENABLED", "").strip().casefold()
        in _ENABLED_TRUE_VALUES
    )
    allow_raw = os.environ.get(f"{prefix}_ALLOW", "")
    allow = [p.strip() for p in allow_raw.split(",") if p.strip()]
    return {"enabled": enabled, "allow": allow}


def _require_authorized(name: str, prefix: str, verb: str) -> None:
    """Raise ``PermissionError`` unless *prefix*'s policy (see
    ``_policy_from_env``) authorizes *verb* against session *name*.

    The message names the exact two environment variables an operator
    would set -- deliberately actionable for whoever is debugging a
    refusal, since the calling agent itself cannot grant this by any
    argument on the tool call.
    """
    policy = _policy_from_env(prefix)
    if keys.destructive_action_allowed(name, policy):
        return
    raise PermissionError(
        f"{verb}({name!r}) refused: not authorized by this server's "
        f"deny-by-default policy. The operator who launched this MCP "
        f"server must opt in via {prefix}_ENABLED=true and "
        f"{prefix}_ALLOW=<comma-separated glob patterns matching session "
        f"names> in this process's environment -- this cannot be granted "
        f"by the calling agent."
    )


@mcp.tool()
async def start(name: str, command: str | None = None, cwd: str | None = None) -> dict:
    """Create a new, detached tmux session.

    WHEN TO USE: you need a new named tmux session running a command (a
    dev server, a build, a long task) that keeps running after this call
    returns.

    ARGS:
        name: session name. REJECTED (returned as ``ok=False``, see
            RETURNS) if it fails tmux-kit's name-charset check, or if it
            contains '.' -- tmux 3.4 silently mangles '.' to '_' at
            creation time with no error, which would leave this tool's
            caller unable to find the session again by the name it asked
            for. Also fails ordinarily if it collides with a live session.
        command: initial foreground command. Passed to tmux as ONE
            shell-quoted argument, so ';'/'&&' inside it run INSIDE the
            session, not in a wrapping shell. Omit for a bare shell.
        cwd: start directory (tmux's own -c flag).

    RETURNS: {"ok": bool, "error": str | null}. ``ok`` is False (not a
    raised exception) for an ordinary failure -- including an invalid or
    mangle-prone NAME, or "command not on PATH" -- check it explicitly.
    """
    try:
        ok, err = await api.start(name, command, cwd=cwd)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
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

    AUTHORIZATION (deny-by-default): refused with `PermissionError` unless
    this server's operator set TMUX_KIT_MCP_STOP_ENABLED=true and
    TMUX_KIT_MCP_STOP_ALLOW=<comma-separated glob patterns matching NAME>
    in the environment this process was launched with -- there is no
    argument on this call that can grant that authorization itself. See
    this module's docstring for exactly what this fence covers and does
    not.

    RETURNS: a short confirmation string. Raises `PermissionError` if NAME
    is not authorized (see above); otherwise raises if the session
    doesn't exist or tmux is unreachable.
    """
    _require_authorized(name, _STOP_ENV_PREFIX, "stop")
    await api.stop(name)
    return f"sent Ctrl-C to {name!r}"


@mcp.tool()
async def kill(name: str) -> str:
    """Kill a session outright (tmux kill-session) -- immediate, hard stop.

    Use `stop` instead if you want to ask the running command to exit
    gracefully while keeping the session around.

    AUTHORIZATION (deny-by-default): refused with `PermissionError` unless
    this server's operator set TMUX_KIT_MCP_KILL_ENABLED=true and
    TMUX_KIT_MCP_KILL_ALLOW=<comma-separated glob patterns matching NAME>
    in the environment this process was launched with -- configured
    INDEPENDENTLY of `stop`'s policy (an operator may permit a wider set
    of sessions to be interrupted than to be destroyed outright). There is
    no argument on this call that can grant that authorization itself. See
    this module's docstring for exactly what this fence covers and does
    not.

    RETURNS: a short confirmation string. Raises `PermissionError` if NAME
    is not authorized (see above); otherwise raises if the session
    doesn't exist or tmux is unreachable.
    """
    _require_authorized(name, _KILL_ENV_PREFIX, "kill")
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
