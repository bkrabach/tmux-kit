# Changelog

All notable changes to `tmux-kit` are documented here. 0.x semantics --
no semver promise; see AGENTS.md's "Versioning is lockstep with muxplex".

## 0.3.1

A manual, direct-call security review of 0.3.0's new deny-by-default MCP
fence found a fail-closed hole: the fence itself was not total.

### Fixed

- **`tmux_kit.keys.destructive_action_allowed()` and
  `input_allowed_for_session()` raised `AttributeError` instead of denying
  when called with `policy`/`settings=None` (or any other non-``dict``
  shape -- a bare string, list, or int).** `policy=None` -- "no policy
  configured" -- is the most likely input in a realistic production
  deployment (an operator who hasn't set any of this server's env vars).
  A fence whose unconfigured path *raises* is not fail-closed in practice:
  whether the caller ends up denying or granting the action then depends
  on that caller's exception handling, which is exactly the ambiguity
  this fence exists to remove. Both functions now check `isinstance(...,
  dict)` up front and return `False` for any non-dict input, in addition
  to the existing checks (missing keys, non-``True`` `enabled`, non-list
  `allow`). `input_allowed_for_session()` had the identical hole --
  `destructive_action_allowed()` generalizes it and inherited the bug
  unchanged; both are fixed together since they share the fail-closed
  contract by design. No production exploit existed via the MCP surface
  itself: `mcp_server._policy_from_env()` always builds a well-formed
  `{"enabled": bool, "allow": list}` dict from the environment (even when
  every env var is unset), so `stop`/`kill` already refused cleanly with
  `PermissionError` today. The hole was real, however, for any direct
  caller of the public `tmux_kit.keys` functions -- a future MCP call
  site, a second consumer, or a test harness that passes `None`/a
  malformed shape when no policy/settings exist. The existing contract is
  unchanged: `enabled`/`input_enabled` must still be the literal `True`,
  `allow`/`input_allowed_sessions` must still be a list matched via
  case-insensitive glob, and authorization still comes from
  operator-supplied environment variables only.

### Added

- Full malformed-input matrix test coverage for both fence functions
  (`tests/test_keys.py`): `None`, `{}`, wrong top-level types (str/list/
  int), missing keys, non-`True` `enabled` values, non-list `allow`
  values, and non-string entries inside `allow` -- every case asserts
  `False`, never an exception.

## 0.3.0

A six-lens product review of the 0.2.x facade/CLI/MCP surface found three
real defects (not opinions): an unguarded destructive-verb surface on the
MCP server, a race between `start()` returning and a session's pane
actually having output, and an inconsistency between `start()` and
`rename()`'s name-mangling guards. All three closed below.

### Fixed

- **The MCP server's `stop`/`kill` tools had no authorization check at
  all.** `tmux_kit.mcp_server`'s destructive lifecycle verbs called
  `tmux_kit.api.stop()`/`kill()` directly against any session name reachable
  via `list_sessions`, with no policy or allowlist gate -- the exact
  failure mode of an agent with raw tmux access, applied to precisely the
  surface (MCP) that hands tmux control to that class of caller with no
  human reviewing each call. This is not a theoretical risk: see AGENTS.md's
  "`TMUX_TMPDIR` is not an isolation boundary" for a real incident, same
  day, in which a hand-rolled tmux command destroyed 73 live sessions.
  `stop`/`kill` are now deny-by-default, gated by an operator-supplied
  policy (`TMUX_KIT_MCP_STOP_ENABLED`/`_ALLOW`,
  `TMUX_KIT_MCP_KILL_ENABLED`/`_ALLOW` environment variables -- see
  `tmux_kit/mcp_server.py`'s module docstring), never grantable by the
  calling agent itself. `stop` and `kill` are independently configurable
  (different blast radius: recoverable vs unrecoverable). This is a
  NARROW, MCP-scoped fence (`tmux_kit.keys.destructive_action_allowed()`,
  generalizing the existing `input_allowed_for_session()` fence) --
  explicitly NOT the fuller, still-deferred `Sender`/`SendPolicy` typed
  authorization object (CONSUMERS.md's "NOT in the library yet"). The CLI
  and any direct library call remain exactly as unguarded as before --
  see the module docstring for the full, honest coverage boundary
  (one global policy per server process, not per client; strength is
  exactly the operator's glob choice).
- **`start()` returned before the spawned command had produced any
  output**, so a `read()` immediately afterward -- the obvious usage this
  library's own quickstart demonstrates -- could return an empty string
  even though the command was about to print something. A new user reads
  that as "it printed nothing" and concludes the library is broken.
  `api.start()` now best-effort waits (bounded, ~0.5s, polling every
  ~50ms) for the pane to show output or for the command to have already
  exited, whichever comes first, before returning on a successful spawn.
  This is honest best-effort, not a guarantee: a command that takes
  longer than the budget to print anything still reads back empty
  immediately afterward, exactly as before -- there is no tmux-native
  "it printed something" event to wait on, only the pane's own content,
  so a bounded poll narrows the race for the common case without
  pretending to close it for an arbitrarily slow command. See
  `tmux_kit.api._wait_for_pane_ready()`'s docstring.
- **`start()` lacked the name-mangling guard `rename()` already had.**
  `rename()` rejects a `new_name` up front if it fails
  `names.is_valid_session_name()` or would be silently mangled by tmux
  (`names.is_tmux_stable_name()` -- tmux 3.4 turns '.' into '_' with exit
  code 0 and no error); `start()` performed no such check, so a session
  created with a dot in its name (e.g. `build.js`) got silently renamed
  by tmux, and every later lookup by the original name missed it.
  `start()` now runs the identical guard, raising `ValueError` before any
  tmux call -- consistent with `rename()`. The lower-level
  `spawn.spawn_session()` primitive is UNCHANGED (still unvalidated, by
  its own documented contract) -- this only tightens the facade's own
  `start()` entry point, not the primitive muxplex calls directly.

### Added

- `tmux_kit.keys.destructive_action_allowed(name, policy)` -- the
  deny-by-default allowlist fence described above.

### Changed

- CLI (`tmux-kit start`) and MCP (`start` tool) both now surface a
  `ValueError` from `api.start()`'s new name validation through their
  existing failure conventions (CLI: exit 1 with reason on stderr; MCP:
  `{"ok": False, "error": ...}`) -- neither surface's documented contract
  for an "ordinary failure" changed shape.

### Considered, not built

- **Exposing `tmux_kit.isolation.isolated_tmux_server()` via the CLI or
  MCP server.** A reviewer noted this primitive -- the one thing that
  would have prevented the incident class this release's MCP fence closes
  -- is unreachable from either surface today. Left out of this change
  deliberately: it doesn't fit the CLI/MCP's existing verb set (both
  operate on the caller's own persistent, named sessions; this primitive
  spins up and tears down a throwaway, differently-socketed server for
  probing/testing), and its own interface shape (what does "run an
  isolated tmux session via MCP" even return -- a live handle an agent
  holds across calls?) is a real design question, not a fence to bolt on
  under this change's time budget. Worth its own follow-up.

### Verified

- Full suite (`uv run pytest -v`), all extras installed: unit + real-tmux
  integration + differential, including a NEW real-tmux integration test
  reproducing the exact start()-then-read() race end-to-end (see the
  report for captured output).

## 0.2.2

CI regression: `test-macos` failed all four `tests/test_isolation.py` real-tmux
tests with `RuntimeError: error connecting to /private/var/folders/.../T/
tmux-kit-isolated-dir-.../tmux-501/tmux-kit-isolated-... (File name too
long)`. Root cause confirmed: AF_UNIX `sockaddr_un.sun_path` is capped at
104 bytes on macOS (108 on Linux). `isolated_tmux_server()`'s private,
defense-in-depth `TMUX_TMPDIR` directory (added in 0.2.1) was created under
`tempfile.gettempdir()`, which on macOS resolves to a long, per-user,
per-boot path (`/var/folders/<random>/T`, 50+ bytes on its own). Once tmux
appended its own `tmux-<uid>/<socket-name>` suffix, the full path blew past
104 bytes on every real macOS invocation; Linux never showed the failure
because `/tmp` is short there by default. Confirmed on a real Mac
(`ssh brians-macbook-pro-os`): the CI-reported path was 127 bytes.

Re-verified the precedence rule from 0.2.1 still holds and is the actual
isolation guarantee: an explicit `-L` alone already overrides an inherited
`$TMUX`; the private `TMUX_TMPDIR` is defense-in-depth on top of that, not
the boundary itself. This release fixes the defense-in-depth layer without
touching the guarantee.

### Fixed

- **`isolated_tmux_server()`'s private `TMUX_TMPDIR` now anchors under
  `/tmp`** (via the new `_short_tmp_base()`, falling back to
  `tempfile.gettempdir()` only if `/tmp` genuinely doesn't exist) instead of
  the platform temp dir, keeping the resulting socket path well under the
  104-byte macOS `sun_path` cap on both platforms. With the default prefix
  the real path length is ~75 bytes (was 127 on the failing macOS CI run).
- **New fail-loud guard**: before ever invoking tmux, the constructed
  socket path (`<TMUX_TMPDIR>/tmux-<uid>/<socket-name>`, matching tmux's
  own construction) is checked against a safety-margined bound
  (`_MAX_SOCKET_PATH_BYTES = 104 - 8`, enforced unconditionally regardless
  of which platform is running the code -- Linux's looser 108-byte limit
  is never used as the check, so a path that would only break macOS can't
  slip through on a Linux-only dev/test run). A caller-supplied `prefix=`
  long enough to blow the bound now raises `ValueError` immediately, naming
  the offending path and byte count, instead of failing three layers down
  inside a tmux subprocess with a bare "File name too long".

### Added

- `tests/test_isolation.py`: `test_default_prefix_socket_path_stays_within_macos_sun_path_bound`
  pins the default-prefix path length as a regression guard (checked
  against the tighter macOS bound even when the suite runs on Linux, per
  above); `test_overlong_prefix_raises_before_ever_touching_tmux` and two
  supporting unit tests for the new `_short_tmp_base()` / `_tmux_socket_path()`
  helpers.

### Verified

- Full suite (`uv run pytest -v`): 160 passed, 2 skipped, on both Linux
  and a real macOS host (`ssh brians-macbook-pro-os`, macOS 26.6, tmux
  3.6a) -- including all four previously-failing `test_isolation.py`
  real-tmux tests.

## 0.2.1

A real incident: an agent probing tmux's `remain-on-exit` behavior set
`TMUX_TMPDIR` and believed that isolated it from the operator's real tmux
server. It did not -- the probe was itself running inside a tmux pane, so
`$TMUX` was set, and tmux prefers an inherited `$TMUX` over `TMUX_TMPDIR`
whenever no explicit `-L`/`-S` is given. `tmux list-sessions` printed 73
real sessions; `tmux kill-server` destroyed all of them. See AGENTS.md's
"`TMUX_TMPDIR` is not an isolation boundary" for the verified mechanism.

A library-only guard (e.g. hardening `run_tmux()`) would have missed this
entirely -- the kill never went through `tmux_kit`; it was a hand-written
shell command. This release instead provides a primitive worth reaching
for, plus a structural CI rail across the whole repo.

### Added

- **`tmux_kit.isolation.isolated_tmux_server()`** -- an async context
  manager yielding an `IsolatedTmuxServer` (`.run(*args)`) bound to a
  unique, throwaway `-L` socket, with `$TMUX` scrubbed from the child
  environment and its own private `TMUX_TMPDIR`, torn down (kill-server +
  directory removal) even if the `async with` block raises. THE tool to
  reach for whenever a test, example, script, or agent needs to poke real
  tmux behavior without any chance of touching an ambient/production
  server. Stdlib-only, core module (covered by the import-purity rail).
- **`tests/test_rails.py::test_tests_and_examples_never_invoke_tmux_without_explicit_isolation`**
  -- a structural (AST-based) rail: fails the build if any test, example,
  or script anywhere in this repo (everywhere except `tmux_kit/`'s own
  production contract) spawns a real `tmux` subprocess without a literal
  `-L`/`-S` in that same call. Recursive from the repo root (not a fixed
  `[tests/, examples/]` list) so coverage survives code moving to a new
  directory later (the same lesson `test_exactly_one_run_shell_construction_site_exists`
  already encodes for the `run-shell` rail).
- `tests/test_isolation.py` -- unit + real-tmux coverage for the new
  primitive, including a reproduction of the exact incident mechanism (a
  crafted `$TMUX` pointing at a throwaway "fake ambient" server) that never
  touches any real/ambient/production tmux server.

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
