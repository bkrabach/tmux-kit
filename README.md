# tmux-kit

Async tmux session-management primitives for Python. The base package is
**stdlib only** — `dependencies = []`, and an AST rail in the test suite
fails the build if any core module imports anything outside the standard
library or `tmux_kit` itself. No fastapi, no httpx, no server stack.
Configuration is injected, never read: no function here knows a settings
file exists.

Your agent's tmux session shouldn't die because you closed your laptop.

tmux-kit spawns and tracks tmux sessions that survive the process that
started them — as a library, a CLI, and an MCP server, all driven by one
identical set of verbs. That guarantee is **unconditional** for the cases
you hit daily: your own process exits, your terminal disconnects, your SSH
connection drops. The tmux server daemonizes away from whatever
spawned it, so none of that reaches it. It is **conditional** for one
harder case — a supervisor restarting the service your process runs under.
On Linux, systemd's
default `KillMode` SIGKILLs everything still in a restarted unit's cgroup,
and a tmux server spawned from that unit is in it; tmux-kit's answer is to
launch the server in its own transient scope (`systemd-run --user
--scope`), which needs a usable systemd `--user` session — something most
sandboxed agent containers don't have. Run `tmux-kit doctor` to see which
case you're in.

## Quickstart

Two scripts, two separate processes. The first one creates a session and
exits; the second, run afterward in a fresh interpreter, finds it and
reads it back:

```python
# quickstart_start.py -- creates the session, then exits
import asyncio
import tmux_kit

asyncio.run(tmux_kit.start("quickstart-demo", "echo hello from tmux-kit; sleep 300"))
```

```python
# quickstart_read.py -- a FRESH process, run afterward, finds and reads it
import asyncio
import tmux_kit

async def main():
    sessions = await tmux_kit.list_sessions()
    print("still running:", [s.name for s in sessions])
    print("it printed:", await tmux_kit.read("quickstart-demo"))

asyncio.run(main())
```

Runnable, tested copies of both live in `examples/`. Importing `tmux_kit`
wires the defaults for you — a dedicated socket directory and the
env-factory plumbing — so a first-time consumer needs to know nothing
about `set_env_factory()` or `tmux_env()` to get this far. The caveat that
matters most: that dedicated socket directory is **not** your ambient tmux
server. Sessions created this way will not appear in a bare `tmux ls`, and
`list_sessions()` returning `[]` means "nothing on tmux-kit's own socket,"
never "nothing running on this host." Override the location with the
`TMUX_KIT_SOCKET_DIR` environment variable or an explicit
`tmux_kit.configure(socket_dir=...)`; an advanced consumer that wants full
control keeps calling `tmux_kit.proc.set_env_factory()` directly, and the
facade backs off entirely when it finds a factory already installed.

Before trusting any of this in a new environment, ask it:

```console
$ tmux-kit doctor
tmux_found: True
tmux_version: tmux 3.4
cgroup_mode: scope-candidate
cgroup_escape_ready: True
socket_dir: /home/you/.local/state/tmux-kit/sockets
socket_dir_writable: True
```

`cgroup_mode: scope-candidate` with `cgroup_escape_ready: True` is the
good case for the conditional guarantee above: a systemd `--user` session
is present and the transient-scope escape has been probed and works.
Where there is no usable systemd `--user` session — macOS, and most
containers — `cgroup_mode` comes back `not-applicable` and `doctor`
appends a note ending "does not apply here, and nothing needs to be done
about it." Read that narrowly. It is scoped to exactly one hazard, the
cgroup-adoption kill described below; it is not a statement that your
sessions are safe from everything, and a container runtime that tears down
the whole container takes the tmux server with it either way.

`doctor` never raises for an environment problem — every check degrades to
a field plus a human-readable note, so you can show its output to a user
(or hand it to an agent) without a try/except. `--json` returns the same
`DoctorReport` shape the library does.

## The incident

tmux resolves which server to talk to in a fixed order: `-S`, then `-L`,
then an inherited `$TMUX`, then `TMUX_TMPDIR`. `$TMUX` outranks
`TMUX_TMPDIR`, so a command that sets `TMUX_TMPDIR` and passes no
`-L`/`-S` is not isolated at all when it runs inside a tmux pane — which
an agent almost always does. During this library's own development, a
hand-written probe of ours did exactly that, believing the fresh
`TMUX_TMPDIR` had isolated it. Its own `tmux list-sessions` printed the
operator's 73 real sessions; a `kill-server` two lines later destroyed all
of them.

Recovery was partial, and it worked as designed. `tmux_kit.presence`
records sessions by positive observation only — never a TTL, never a
sweep — so it read the server-identity change as a cold start, refused to
tombstone anything, and froze all 71 recoverable sessions into
`pending_restore`. A later restore brought 40 back and refused 12 more —
each because the session's real working directory wasn't where the default
command would have started it, naming the true path instead of doing the
wrong thing quietly.

Two things shipped in response (0.2.1). `isolated_tmux_server()`: an async
context manager giving a unique `-L` socket, a private `TMUX_TMPDIR`, a
scrubbed `$TMUX`, and guaranteed teardown even if the body raises. And an
AST-based CI rail in `tests/test_rails.py` that fails the build if any
test, example, or script in this repo spawns tmux without a literal
`-L`/`-S` in the same call. The rail is proven to fire, not only to pass:
its detector was checked against deliberately violating call sites in each
of the three argv shapes it scans, and it flags all three.

What that still does not cover: the rail reads this repo only. It cannot
see a consumer's code, and it cannot see a command an agent hand-writes
into a shell — which is precisely what caused this. The MCP
deny-by-default fence below is a separate, narrower, later mitigation for
a different risk, and would not have prevented this one.

**One disclosure, because a skeptic will find it in sixty seconds
anyway.** `proc.tmux_env()` sets `TMUX_TMPDIR`, and `run_tmux()` never
passes `-L`/`-S` — this library's own production path uses the very
mechanism the story above calls unsafe. It is safe *only* because the same
function calls `env.pop("TMUX", None)` (`proc.py`, marked load-bearing in
its docstring). There is no environment shape in which an inherited
`$TMUX` can leak through and win, because the child never sees it. That
one line is the entire difference between this library and the script that
destroyed 73 sessions.

```python
from tmux_kit import isolated_tmux_server  # or tmux_kit.isolation.isolated_tmux_server

async with isolated_tmux_server() as server:
    await server.run("new-session", "-d", "-s", "probe")
    out = await server.run("list-sessions")
# kill-server + directory removal happen here, even if the body raised
```

Three older production incidents shaped the code too, each pinned by its
own test: a diagnostic `run-shell` that painted curl errors across 53 live
sessions; 52 sessions lost to a presence TTL sweep; 44 SIGKILLed when a
service restart took the tmux server its cgroup had adopted. `AGENTS.md`
carries the writeups.

## One vocabulary, three doors in

The verbs in `tmux_kit.api` — `start`, `list_sessions`, `status`,
`exit_code`, `read`, `page`, `search`, `wait_for_attention`, `stop`,
`kill`, `rename`, `doctor` — are reused by the CLI and the MCP server,
which are thin argument-marshalling wrappers over those exact functions.
Fix or extend a capability in the facade and the other two surfaces
inherit it. `--json` on the CLI's structured read commands (`list`,
`status`, `exit-code`, `page`, `search`, `doctor`) emits the same dataclass
shapes an `import tmux_kit` caller gets back.

Two honest exceptions to "identical names." `is_running` exists only in
the library (it is a convenience wrapper around `status`, not a separate
call) — there is no CLI command and no MCP tool for it. And the CLI
shortens three verbs for a terminal: `list_sessions` → `list`,
`wait_for_attention` → `wait`, `exit_code` → `exit-code`. MCP keeps all
three at full length.

Both extras are opt-in and neither adds a dependency to the base package:

```bash
pip install 'tmux-kit[cli]'   # -> the `tmux-kit` command (Click)
pip install 'tmux-kit[mcp]'   # -> the `tmux-kit-mcp` stdio server (MCP SDK)
```

```console
$ tmux-kit start build --command "sleep 300"
started 'build'
$ tmux-kit list
build	running
$ tmux-kit status build
running
```

Every command's `--help` is written to be read cold by an agent with no
other context: what it does, when to reach for it, what it returns, what
fails and why, and its exit codes.

**MCP server: `stop`/`kill` are deny-by-default (0.3.0).** An MCP client
is an unsupervised agent by construction, so the two destructive lifecycle
verbs refuse every call with `PermissionError` unless the operator who
launches the server opts in explicitly, in the environment the process is
launched with — never by a parameter on the tool call itself:

```bash
TMUX_KIT_MCP_STOP_ENABLED=true TMUX_KIT_MCP_STOP_ALLOW='demo-*' \
TMUX_KIT_MCP_KILL_ENABLED=true TMUX_KIT_MCP_KILL_ALLOW='demo-*' \
  tmux-kit-mcp
```

`stop` and `kill` are independently configurable, so an operator can
permit a wider blast radius for the recoverable verb than the
unrecoverable one. An unset, misspelled, or non-`true`/`1`/`yes`
`_ENABLED` value refuses every call for that verb regardless of `_ALLOW`.
Read `tmux_kit/mcp_server.py`'s module docstring before assuming the fence
is total — it gates only these two MCP tools (the CLI and any direct
library call remain exactly as unguarded as before), it is one global
policy per server *process* rather than per connected client, and its
strength is exactly the operator's glob choice: an allowlist of `*`
authorizes every session name and protects nothing.

## Sharing one tmux server between two apps

The tmux server is a shared singleton, and some of its state is a single
global slot. If you point a second consumer at a socket another app
already uses, four things bite:

- **The `alert-bell` hook is one slot, last writer wins, silently.** Two
  apps arming it means one of them stops receiving bells with no error.
- **Presence cross-talk.** A cold start observes every session on the
  socket, not just yours — so an unscoped observation freezes the *other*
  app's sessions into your restore plan.
- **Fence overlap.** The MCP allowlist fence matches glob patterns against
  session *names*, so on a shared socket your globs can authorize the other
  app's sessions.
- **Session-name collisions**, which surface as a spawn failure at the
  worst possible moment.

This is why `default_socket_dir()` resolves to
`$XDG_STATE_HOME/tmux-kit/sockets` and deliberately not to tmux's own
ambient default. Point at a shared server only via an explicit
`configure(socket_dir=...)`, made with all four hazards in mind;
`tmux_kit/CONSUMERS.md` documents each in full.

## Extending it

`tmux_kit/CONSUMERS.md` is the single canonical enumeration of every
low-level module and the functions it exports — the facade above is an
additive convenience layer, and `proc`, `spawn`, `observe`, `presence`,
`bell`, `names`, `keys`, `cgroup`, and `lifecycle` remain fully usable on
their own. It is kept as ONE hand-maintained list rather than two: a
second, drifted copy in this README once documented a function that had
never existed, and a first-time reader who copied it got an `ImportError`
on their first call.

Improvements flow back as PRs against this repo, never as a copy into a
consumer.

## Versioning

0.x — no semver promise yet. See `CHANGELOG.md` for what each release
changed. Pin exactly:

```toml
dependencies = ["tmux-kit==0.3.2"]

# Pinned git install, for a managed environment that cannot reach public
# PyPI (see CONSUMERS.md):
#   tmux-kit @ git+https://github.com/bkrabach/tmux-kit@v0.3.2
```

## Tests

```bash
uv sync --extra dev
uv run pytest
```

That gives `210 passed, 2 skipped` on this tree — the two skips are the CLI
and MCP test modules, which `importorskip` their extras. Install those to
run all 234:

```bash
uv sync --extra dev --extra cli --extra mcp
uv run pytest
```

Several of those are incident tests, not tests written against a spec —
`test_presence.py`, `test_cgroup_escape.py`, `test_isolation.py`,
`test_rails.py`, and `test_differential_harness.py` each carry assertions
that exist because a specific thing happened in production. When one
fails, the fix is essentially never to weaken the assertion. Beyond the
unit suite there is a differential harness (`pytest -m differential`, 22
tests, replaying fleet-recorded real-tmux data) and a real-tmux
integration suite (`pytest -m integration`, 15 tests, against an isolated
`-L` socket). CI (`.github/workflows/test.yml`) runs the full suite
including both markers unconditionally — a CI runner has no live sessions
to endanger — across Python 3.11/3.12/3.13 on Linux, plus an extras job
and a macOS job.

---

Extracted from [muxplex](https://github.com/bkrabach/muxplex), which
remains its first consumer and the archaeological record for this
library's pre-extraction history.
