# tmux-kit

Async, argv-exec tmux session-management primitives, extracted from
[muxplex](https://github.com/bkrabach/muxplex) (extraction design:
`docs/plans/2026-08-08-tmux-lib-extraction-plan.md`; the move to this
repo + PyPI: `docs/plans/2026-08-09-tmuxkit-own-repo-and-pypi-plan.md` --
both live in the muxplex repo, which remains the archaeological record for
this library's pre-2026-08-08 history).

Named for tmux, not for muxplex: this is a library about a tmux server,
usable by any application that manages tmux sessions. muxplex is its first
consumer. Since the split, each project has its own repo, its own version
line, and its own release cadence -- muxplex pins an exact `tmux-kit==`
version and bumps it deliberately (see the pyproject.toml note in this
repo, and the muxplex repo's own dependency declaration).

**stdlib only** (the base package -- see "Optional extras" below for the
CLI/MCP surfaces). No fastapi, no httpx, no server code. Configuration is
injected, never read (§4.3): no function in this package knows that a
settings file exists.

## Quickstart

The facade (`import tmux_kit`) wires sensible defaults -- a dedicated
default socket directory, the env-factory plumbing -- so a fresh consumer
needs zero knowledge of `set_env_factory()`/`tmux_env()` to get going. Two
scripts, two separate processes, proving a spawned session survives the
launching process's exit:

```python
# spawn.py -- creates the session, then exits
import asyncio
import tmux_kit

asyncio.run(tmux_kit.start("demo", "echo hello from tmux-kit; sleep 300"))
```

```python
# read.py -- a FRESH process, run afterward, finds and reads it
import asyncio
import tmux_kit


async def main():
    print(await tmux_kit.list_sessions())
    print(await tmux_kit.read("demo"))


asyncio.run(main())
```

Runnable, tested versions of both live in `examples/` (`quickstart_start.py`
/ `quickstart_read.py`) -- see their module docstrings.

An advanced consumer (muxplex is the reference implementation) that wants
full control keeps using `tmux_kit.proc.set_env_factory()` and the
low-level modules directly; the facade only installs its default when
nothing else has been wired (see `tmux_kit/api.py`'s module docstring).

**Full facade verb reference:** `start`, `list_sessions`, `status`,
`is_running`, `read`, `page`, `search`, `wait_for_attention`, `stop`,
`kill`, `rename`, `doctor` -- see `tmux_kit/api.py`'s docstrings (each
function documents its own contract; this file does not duplicate them).

**Full low-level module/function reference:** `tmux_kit/CONSUMERS.md` is
the single canonical enumeration of every module and the functions it
exports -- kept here as ONE hand-maintained list instead of two, after a
second, drifted copy of it in this README caused a real
function-doesn't-exist bug for a first-time reader (see CHANGELOG 0.2.0).

## Optional extras: CLI and MCP server

Both are thin wrappers over the exact same facade verbs above -- same
names in the CLI, the MCP tool descriptions, and `tmux_kit.api`. Neither
adds a dependency to the base package; each is its own extra:

```bash
pip install 'tmux-kit[cli]'   # -> the `tmux-kit` command (Click)
pip install 'tmux-kit[mcp]'   # -> tmux_kit.mcp_server:main (MCP stdio server)
```

**MCP server: `stop`/`kill` are deny-by-default (0.3.0).** An MCP client is
an unsupervised agent by construction, so the two destructive lifecycle
verbs refuse every call with `PermissionError` unless the operator who
launches the server opts in explicitly:

```bash
TMUX_KIT_MCP_STOP_ENABLED=true TMUX_KIT_MCP_STOP_ALLOW='demo-*' \
TMUX_KIT_MCP_KILL_ENABLED=true TMUX_KIT_MCP_KILL_ALLOW='demo-*' \
  tmux-kit-mcp
```

`stop` and `kill` are independently configurable. See
`tmux_kit/mcp_server.py`'s module docstring for exactly what this fence
covers (only these two MCP tools) and does not (the CLI, and any direct
library call, remain unguarded, as before).

```bash
tmux-kit --help               # every command documents itself for an
tmux-kit start demo --command "npm run dev"   # agent reading --help cold
tmux-kit list --json
```

## Sharing one tmux server between two apps

The tmux server is a shared singleton and some of its state is a single
global slot. Before shipping a second consumer on a host that also runs
muxplex, read plan §17 — the `alert-bell` hook slot (last writer wins,
silently), presence cross-talk (scope your observations or your cold start
freezes the *other* app's sessions into your restore plan), fence overlap,
and session-name collisions.

## Versioning

0.x semantics — no semver promise yet. First release was `0.1.0` (the
0.44.0 numbering used inside the muxplex monorepo was a pin-repair
artifact that the rename to `tmux-kit` voided; see
`docs/plans/2026-08-09-tmuxkit-own-repo-and-pypi-plan.md` §4 in the
muxplex repo for the full reasoning). See `CHANGELOG.md` for what each
release added. Note the PyPI distribution name uses a hyphen (`tmux-kit`)
while the Python import package uses an underscore (`tmux_kit`), because
hyphens are illegal in Python identifiers (cf. `python-dateutil` ->
`dateutil`):

```toml
# Public installs (primary path):
dependencies = ["tmux-kit==0.2.0"]

# Pinned git install (e.g. a managed environment that cannot reach
# public PyPI -- see CONSUMERS.md):
#   tmux-kit @ git+https://github.com/bkrabach/tmux-kit@v0.2.0
```

```python
import tmux_kit
```

Improvements flow both ways as PRs against this repo — never a copy into a
consumer (a file in a consumer that is byte-similar to a file here is the
incident this whole extraction exists to prevent, regardless of intent).

## Tests

This repo's own `tests/` directory carries every incident test that moved
with the code (the 44/52-lost-session presence rule, the multi-window bell
finding, the `.`->`_` mangling refusal, the casefold+fnmatchcase allowlist
fence, the cgroup-escape guards), plus the differential harness
(`pytest -m differential`, replayed against fleet-recorded real-tmux data),
a real-tmux integration suite (`pytest -m integration`, isolated `-L`
socket), and unit coverage for the 0.2.0 additions (`observe.pane_is_dead`,
`bell.wait_for_bell`, `lifecycle`, the `tmux_kit.api` facade). Run the full
suite locally:

```
uv sync --extra dev
uv run pytest
```

To also exercise the optional CLI/MCP surfaces' own tests:

```
uv sync --extra dev --extra cli --extra mcp
uv run pytest
```

CI (`.github/workflows/test.yml`) runs the full suite -- including
`-m integration` and `-m differential` unconditionally, since a CI runner
has no live muxplex to endanger -- on Python 3.11/3.12/3.13, Linux and
macOS.
