# Changelog

All notable changes to `tmux-kit` are documented here. 0.x semantics --
no semver promise; see AGENTS.md's "Versioning is lockstep with muxplex".

## 0.2.0

Three independent reviews of `tmux-kit` 0.1.0 named the same blocker: no
runnable example anywhere. Building one surfaced the real problem --
a correct low-level surface with no usable entry path for a fresh
consumer -- and everything below traces back to that finding.

### Fixed

- **`spawn.spawn_session()`'s `env` default silently ignored an installed
  env factory.** Every other function in this package (`run_tmux()` and
  everything built on it) consults `proc.set_env_factory()` when `env` is
  omitted; `spawn_session()` alone defaulted to a bare `None` ("always
  inherit the ambient environment"), a divergence invisible to muxplex
  (which always passes `env=` explicitly) but a real footgun for a new
  consumer who installs a factory and expects it to apply everywhere. This
  is what produced the reported failure: a session spawned onto the
  AMBIENT socket while a subsequent `enumerate_sessions()` (which does
  consult the factory) looked for it on the configured one and found
  nothing -- "still running: []" followed by a "no server running"
  `RuntimeError`. `spawn_session()`'s `env` parameter now shares the same
  omitted-vs-`None` sentinel (`proc.UNSET`) as `run_tmux()`. Byte-identical
  behavior for any caller (muxplex included) that always passes `env=`
  explicitly.
- README documented `names.rename_session` -- that function has never
  existed; the real name is `rename_tmux_session()`. A reader who copied
  the README's example got an `ImportError` on the first call. `cgroup`'s
  documented exports also drifted between README and CONSUMERS.md.
  `tmux_kit/CONSUMERS.md` is now the ONE hand-maintained enumeration of
  the low-level surface; README defers to it instead of carrying a second,
  driftable copy.

### Added

- **The facade (`tmux_kit.api`, re-exported at the top level)** --
  `start`, `list_sessions`, `status`, `is_running`, `read`, `page`,
  `search`, `wait_for_attention`, `stop`, `kill`, `rename`, `doctor`,
  `configure`, `default_socket_dir`. Wires a sensible, zero-configuration
  default (a dedicated socket directory, distinct from tmux's own ambient
  default AND from muxplex's configured one -- see CONSUMERS.md's "Two
  apps, one tmux server" hazard) so a fresh consumer needs zero knowledge
  of env factories or socket directories to get going: `import tmux_kit;
  await tmux_kit.start("demo", "echo hi")`. An advanced consumer (muxplex)
  that calls `tmux_kit.proc.set_env_factory()` itself is completely
  unaffected -- the facade only installs its default when nothing else has
  been wired.
- **`observe.pane_is_dead()`** -- "is it done, or still going?" answered
  via tmux's own `#{pane_dead}` flag. Previously unanswerable without a
  consumer inventing their own tmux incantation.
- **`bell.wait_for_bell()`** -- blocks until a session's bell rings (or a
  timeout elapses), instead of a caller hand-rolling a poll loop around
  the existing `poll_bell_flag()`.
- **`tmux_kit.lifecycle`** (new module) -- `kill_session()` and
  `interrupt_session()`, `spawn_session()`'s missing counterpart. Every
  caller previously had to drop to `run_tmux("kill-session", ...)` by
  hand.
- **Scrollback paging and search, exposed via the facade** --
  `capture_pane_metadata()`/`capture_pane_window()` already implemented
  absolute-line-addressed paging in 0.1.0, but nothing converted a
  caller's request into tmux's relative `-S`/`-E` coordinates for them.
  `api.page()` does that conversion; `api.search()` composes it into a
  scrollback search ("did it print an error?").
- **`api.doctor()`** -- a one-call "will this work here?" preflight,
  composing tmux's presence/version, `cgroup.environment_mode()` (which
  already answered "does the cgroup-escape hazard even apply here" in
  0.1.0 -- e.g. `"not-applicable"` on macOS and in most containers, the
  single biggest unanswered adoption question -- but was undocumented and
  had no one-call entry point), the real cached `cgroup.should_escape()`
  probe, and socket-directory writability.
- **Optional extras: `tmux_kit.cli` (`cli` extra, Click) and
  `tmux_kit.mcp_server` (`mcp` extra, MCP stdio server)** -- both are thin
  wrappers over the exact same `tmux_kit.api` verbs, with `--help`/tool
  descriptions written for an agent reading them cold. Neither adds a
  dependency to the base package, which stays stdlib-only (enforced by
  `tests/test_rails.py`'s now-scoped import-purity rail -- see below).
  `--json` output is available on every read-oriented CLI command.
- Runnable quickstart examples: `examples/quickstart_start.py` /
  `quickstart_read.py` -- two separate processes proving a spawned
  session survives the launching process's exit (this is the change's
  own gate proof; see the report for the actual captured output).

### Rejected (considered, not built)

- A typed `Sender`/`SendPolicy` authorization object. `lifecycle` adds raw
  execute-side primitives (`kill_session`, `interrupt_session`) alongside
  the existing argv builders in `tmux_kit.keys`, but the deny-by-default
  POLICY layer stays deliberately deferred (CONSUMERS.md) -- 0.x holds
  open for the second real consumer to shape it.
- The ttyd/embedded-terminal lifecycle -- still muxplex's, still gated on
  its own embedded human-UX design (unchanged from 0.1.0).

### Changed

- `tests/test_rails.py`'s import-purity rail is now scoped to the CORE
  modules (everything except `cli.py`/`mcp_server.py`); each extra gets
  its own, narrower rail asserting it imports nothing beyond stdlib +
  `tmux_kit.*` + its one named third-party package. This narrows the
  rail's SCOPE, not its protection -- the base package's zero-dependency
  contract is exactly as strict as before.
- `pyproject.toml`'s `mcp` extra is pinned `mcp>=1.2,<2` -- verified live
  that `mcp==2.0.0` removed `mcp.server.fastmcp.FastMCP` (restructured to
  `mcp.server.mcpserver.MCPServer`), which broke this extra at import time
  under an unconstrained `mcp>=1.2`.

## 0.1.0

First release under the `tmux-kit` name, in its own repo. See
`tmux_kit/CONSUMERS.md` and the muxplex repo's
`docs/plans/2026-08-09-tmuxkit-own-repo-and-pypi-plan.md` for the
rename/extraction history.
