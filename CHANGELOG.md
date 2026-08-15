# Changelog

All notable changes to `tmux-kit` are documented here. 0.x semantics --
no semver promise; see AGENTS.md's "Versioning is lockstep with muxplex".

## 0.4.0

### Changed (BEHAVIORAL, not additive -- reads both existing public builders' output)

- **`tmux_kit.keys.build_send_text_argv()` and `build_send_key_argv()` now
  CHAIN `build_exit_copy_mode_argv()` ahead of their `send-keys` call,
  instead of leaving it to the caller to invoke first.** 0.3.6 added
  `build_exit_copy_mode_argv()` as a standalone builder that callers had
  to remember to call before every send -- that was the wrong fix: a
  reminder that decays, and every new consumer could forget it. It
  already had: `lifecycle.interrupt_session()` -- reached via the
  shipped, MCP-exposed `api.stop()` that an AI agent calls to interrupt a
  runaway process -- never called it. As a direct result, **`api.stop()`
  silently failed to deliver Ctrl-C whenever the target pane was in
  copy-mode**, with no error of any kind. Measured on real tmux 3.4: a
  `while true` loop kept running (5 lines -> 9 lines of output over 4
  seconds) after `stop()` was called -- the C-c was consumed by the
  copy-mode key table instead of reaching the process.

  The fix moves the guarantee INTO the two send builders, so no consumer
  -- present or future -- can bypass it. Both builders now return TWO
  tmux commands chained into one argv via a literal `;` element:
  `copy-mode -q -t <target> ; send-keys ...`. This is the same
  chaining-via-literal-`;` mechanism already used by
  `observe.capture_pane_window()`; both commands execute in one tmux
  server command-loop tick, so the exit-and-send is atomic (no window
  for a user to re-scroll in between), and `lifecycle.interrupt_session()`
  inherits the fix for free with no code change of its own.

  `build_exit_copy_mode_argv()` itself is UNCHANGED and remains public --
  the two send builders compose it rather than duplicating its literal
  argv list.

  **Verified the chain does not reopen the literal-send security
  property:** tested against real tmux 3.4 with hostile text containing
  a literal `;`, `$HOME`, and backticks/quotes -- the `;` inside the text
  stayed inside the single, `--`-terminated `send-keys` argument and was
  never parsed as a second tmux command, because `;` only acts as a tmux
  command separator when it is its OWN argv element, never as a substring
  of another element.

  **Callers affected:** any code building on `tmux_kit.keys.build_send_text_argv()`
  / `build_send_key_argv()` (directly, or transitively via
  `lifecycle.interrupt_session()`) will now see the `copy-mode -q -t
  <target> ;` prefix in the argv it passes to `run_tmux()` /
  `create_subprocess_exec`. A test asserting the old, bare `["send-keys",
  ...]` shape must be updated to the new chained shape (see
  `tests/test_keys.py`, `tests/test_lifecycle.py`, and
  `tests/fixtures/differential/recorded.json`, all updated in this
  release).

## 0.3.6

### Added

- **`tmux_kit.keys.build_exit_copy_mode_argv(name)`** -- argv for
  `copy-mode -q -t <target>`, a pure builder alongside the existing
  `build_send_text_argv()` / `build_send_key_argv()`. Motivated by a real,
  measured hazard: `mouse on` puts a pane into copy-mode silently on a
  mouse wheel-up, and a pane in copy-mode routes `send-keys -l` /
  `send-keys Enter` through the copy-mode key table instead of to the
  program running there -- typed text is silently swallowed and never
  delivered (measured on tmux 3.4 with a real config). `send-keys -X -t
  <target> cancel` looks like the fix but is wrong: it exits 1 ("not in a
  mode") on a pane that is NOT already in copy-mode -- the common case --
  so it would raise on every ordinary send. `copy-mode -q` is the safe
  alternative: exit 0 whether or not the pane is in a mode (a no-op when
  it isn't), and it correctly takes `pane_in_mode` from 1 to 0 when it is
  (both measured on tmux 3.4). Uses `session_target()`, the same pane
  target `send-keys` takes, not the `=name` exact-match form.

  Purely additive: a new function alongside the existing argv builders,
  nothing else in `tmux_kit.keys` changed shape. Not exported from
  `tmux_kit/__init__.py` -- consistent with `build_send_text_argv()` /
  `build_send_key_argv()`, neither of which is re-exported at the top
  level either; the facade (`tmux_kit.api`) deliberately does not expose a
  general send API (see CONSUMERS.md's "NOT in the library yet", the
  deferred `Sender`/`SendPolicy` object). Reach it via `from tmux_kit.keys
  import build_exit_copy_mode_argv`, same as its neighbours.

## 0.3.5

### Fixed

- **A second cold start silently discarded un-actioned `pending_restore`
  entries from an earlier cold start -- a real data-loss incident,
  observed TWICE on a live machine in one day.** `presence.py`'s
  DIFFERENT-SERVER (cold start) branch of `update_manifest()` REPLACED
  `pending_restore` wholesale whenever the new cold start itself lost any
  sessions, instead of merging. Sequence: server A dies, N sessions freeze
  into `pending_restore`; an operator restores some of them, the rest
  stay pending because they legitimately refuse (wrong cwd, deleted
  dirs); a second, unrelated server death (e.g. a reboot) fires another
  cold start, and the still-pending entries from generation 1 vanished
  the instant the new cold start had its own newly-lost names to record.
  Verified on the affected host by diffing manifest backups taken by hand
  before each incident (81 sessions lost -> 61 restored, 20 remained
  pending -> a reboot's cold start discarded all 20; a second incident an
  hour later discarded 4 more the same way). This is exactly the silent
  sweep AGENTS.md's "the presence record is POSITIVE -- never a TTL,
  never a sweep" invariant exists to forbid; the bug was in the very code
  meant to enforce it.

  **The fix:** the cold-start branch now MERGES this cycle's newly-lost
  sessions into any already-pending, not-yet-restored snapshot, instead
  of replacing it. Nothing leaves `pending_restore` except by being
  restored or explicitly forgotten (`restore --forget`) --
  `mark_restored()` already handled both; only the cold-start freeze
  itself was destructive.

  - **Collision rule:** a name lost under both the old pending set and
    this cycle's fresh losses takes the FRESHER (this-cycle) observation
    -- it reflects the session's actual state right before its own
    server disappeared, a more accurate snapshot than whatever was frozen
    earlier.
  - **`detected_at`/`lost_epoch` tradeoff:** these describe one detection
    event by construction, but a merged snapshot can now span more than
    one cold start. Taken deliberately: the merged snapshot keeps
    describing the OLDEST still-unresolved entry rather than refreshing
    to "now" on every merge. A downstream staleness gate (e.g. muxplex
    `restore.py`'s `RESTORE_MAX_AGE_SECONDS` check, which reads exactly
    this top-level pair to decide whether to refuse a restore without
    `--force`) would otherwise see a genuinely ancient, deliberately-
    un-actioned entry look perpetually fresh after every subsequent cold
    start -- quietly defeating the one safety net that exists to keep it
    from restoring a stale ghost. The tradeoff: a stale entry mixed into
    a merged batch requires `--force` for the whole batch (a workflow
    speed bump, fully recoverable) rather than the alternative's silent,
    permanent loss of the staleness signal (not recoverable). Per-entry
    timestamps would resolve this more precisely but are a manifest
    schema change; not needed to fix the reported data-loss bug, and the
    coarser default keeps a freshly-frozen entry (the common,
    no-prior-`pending_restore` case) byte-identical to before --
    `tests/test_differential_harness.py`'s
    `test_cold_start_freezes_lost_sessions_verbatim` still pins this.
  - **No manifest schema change.** `pending_restore`'s shape
    (`detected_at`, `lost_epoch`, `sessions`) is unchanged; a manifest
    written by 0.3.4 merges correctly under 0.3.5 with no migration step
    (see `tests/test_presence.py`'s
    `test_second_cold_start_manifest_with_0_3_4_shaped_pending_restore_still_works`).

  Regression coverage: `tests/test_presence.py`'s
  `test_second_cold_start_preserves_un_actioned_pending_restore_from_first`
  reproduces the exact incident sequence (cold start -> partial restore ->
  second cold start with different lost names) and fails against the
  pre-fix code; `test_second_cold_start_fresher_observation_wins_on_name_collision`
  and `test_second_cold_start_keeps_oldest_detected_at_and_lost_epoch` pin
  the two tradeoffs above.

## 0.3.4

### Removed

- **`tmux_kit.labels`** (added in 0.3.3, removed here). It was pane-harness
  detection: `label_session()` / `label_sessions()` pattern-matched process
  argv and pane-snapshot chrome against three hardcoded product names
  (`amplifier`, `claude-code`, `codex`) to guess which AI coding tool ran in
  a session's active pane.

  This is a **scope removal, not a defect fix -- the code worked as
  documented and its adversarial-review-fixed bugs stayed fixed.** It does
  not belong in this library: every other module here answers "how do I
  drive tmux correctly," a question with the same answer for every
  consumer. `labels` answered "which of three specific AI products is this,"
  a question a team running aider, goose, cursor-agent, or a bare shell
  answers differently -- that makes it application policy, not tmux
  mechanism (see AGENTS.md's new "Scope" section for the full litmus test).
  It was also the library's only heuristic (everything else here is
  deterministic) and its own maintenance treadmill: product names, argv
  shapes, and banner text all drift, bolted onto code that should be the
  slowest-moving in the repo.

  Removed: `tmux_kit/labels.py`, `tests/test_labels.py`, and its row in
  `tmux_kit/CONSUMERS.md`'s public-surface table. It was never re-exported
  at the top level (`import tmux_kit`), so no `__init__.py` change was
  needed. Nothing else in this package imported it.

  0.x carries no semver promise (see AGENTS.md); this is exactly the kind
  of change that promise exists to allow. Its natural home is the
  consuming application that needs harness detection -- build it there,
  where the label table can be shaped for whatever tools that team
  actually runs. Anyone pinning `tmux-kit==0.3.3` keeps the module; a
  pin bump to `0.3.4` is what removes it.

## 0.3.3

The library's second consumer (concern-sessions, the "sessions" connector
of the concern engine) contributed `tmux_kit.labels`: pane-harness
detection for a box running many real tmux sessions, each hosting
whatever coding tool its human chose. An adversarial review returned
"merge with changes" -- two proven bugs and one overselling doc claim,
all fixed before merge (below).

### Added

- **`tmux_kit.labels`** (new module) -- `label_session()` /
  `label_sessions()`: which agent harness (`amplifier`, `claude-code`,
  `codex`, or the honest `unknown`) runs in a session's active pane, with
  evidence (`HarnessLabel.source`: `"process"` | `"snapshot"` | `"none"`).
  Process-tree first (walks the pane PID's descendants breadth-first,
  shallowest match wins), narrow snapshot sniff as fallback, never a
  guess -- see CONSUMERS.md for the full surface and the module's own
  docstring for the evidence hierarchy.

### Fixed (found by adversarial review, before merge)

- **`process_table()`'s `ps` call truncated long argv on a real
  terminal.** `ps -A -o args=` (no width flag) derives its column width
  from the process's CONTROLLING TERMINAL via an ioctl, independent of
  whether `ps`'s own stdout is piped. A headless CI runner has no
  controlling terminal (unlimited width, so the bug never showed up
  there), but a real interactive box -- this library's stated target --
  does, and a long argv (a deep tmp/session path, a long project
  directory) got silently truncated, chopping off exactly the basename
  the matcher needs. Reproduced failing under a real controlling
  terminal (`test_fake_harness_labels_from_live_process_tree`), fixed by
  adding `-ww` (unlimited width, accepted by both procps on Linux and BSD
  `ps` on macOS), reproduced passing.
- **`_match_cmdline()` matched a harness name appearing ANYWHERE in
  argv, not just the executable.** Mislabeled routine commands purely
  because a harness name appeared as a git branch, a log path, a
  filename, an env-assignment value, or a backup source/destination:
  `git checkout claude`, `cat /var/log/amplifier`, `vim .../codex`,
  `AMPLIFIER_HOME=/opt/apps/amplifier some_daemon --serve`, `rsync
  .../amplifier ...`. Fixed by scoping the match to the EXECUTABLE
  POSITION only (new `_executable_tokens()` helper), which also
  correctly resolves a shell/interpreter's direct positional target --
  the actual kernel-level shape of a shebang'd script
  (`/bin/sh /path/to/amplifier`, `node /usr/bin/claude`) -- Python's
  `-m <module>`, a task-runner's `run` subcommand (`uv run amplifier`),
  and a nested `sh -c "..."` script, while preserving the
  `amplifier-attention-manager` exact-basename counterexample.
- **`DEFAULT_SNAPSHOT_PATTERNS` matched prose, not just chrome.** The
  module's own docstring claimed only "chrome-shaped signatures" (banner/
  version lines) match, but the patterns were unanchored substring
  searches: "what do you think of Claude Code v2 compared to amplifier?"
  and "OpenAI Codex was announced in 2021" both matched. Fixed by
  anchoring every pattern to the start of a screen line (optional
  leading whitespace, `re.MULTILINE`) -- a real banner IS the line; a
  mid-sentence mention is not.

### Verified

- Full suite (`uv run pytest -v`), all extras installed: 264 passed.
  `-m integration` (17, real isolated `-L` tmux server) and
  `-m differential` (22) both green. CI green across Python
  3.11/3.12/3.13, the extras job, and macOS.

## 0.3.2

Two independent reviews (a seven-lens design council and simulated user
research including an "AI agent" persona) landed the same day as a real
incident in which an agent, believing `TMUX_TMPDIR` isolated it, destroyed
73 of an operator's live tmux sessions via a hand-rolled `tmux` command
(see AGENTS.md). The reviews' core finding: nothing in this library's own
MCP tool descriptions or CLI `--help` told an agent that `list_sessions`
answers "what's running on tmux-kit's own socket," not "what's running on
this host" -- so an empty result reads as authoritative ground truth
instead of scope, and the trained recovery ("let me just check with a
raw `tmux ls`") is exactly the incident's own trigger. Both reviews'
claims were independently verified against source before any of this was
implemented -- not taken on faith.

### Fixed

- **No MCP tool description or CLI `--help` text stated that tmux-kit
  operates on its own private socket directory, never the caller's
  ambient tmux server.** Every observation-oriented tool/command
  (`list_sessions`/`list`, `status`, `read`, `page`, `search`,
  `wait_for_attention`/`wait`, `doctor`, plus `start`/`stop`/`kill`/
  `rename` for the sessions they touch) now states this explicitly,
  where an agent actually reads it (the tool description / `--help`
  text itself, not just the README): the scope is tmux-kit's own
  socket; an empty/missing result means "nothing on THIS socket," never
  "nothing running"; and -- the specific, named prohibition -- never
  fall back to a raw `tmux` command to "double check," because an
  inherited `$TMUX` (present whenever the calling process is itself
  inside a tmux pane, common for coding agents) silently outranks
  `TMUX_TMPDIR` with no explicit `-L`/`-S`, which is exactly the
  mechanism that destroyed the 73 sessions. Each of these call sites
  points at `tmux_kit.isolation.isolated_tmux_server()` as the correct
  tool for a real, throwaway tmux probe instead.
- **`tmux_kit.isolation.isolated_tmux_server()` -- the direct remedy for
  the incident class above -- was not exported from `tmux_kit/__init__.py`**,
  reachable only via `from tmux_kit.isolation import isolated_tmux_server`,
  not the top-level facade path (`import tmux_kit; tmux_kit.start(...)`)
  every other quickstart-documented capability uses. Now also re-exported
  as `tmux_kit.isolated_tmux_server`. CLI/MCP exposure remains
  deliberately NOT built, per 0.3.0's "Considered, not built" entry below
  this one -- that reasoning (a throwaway async context manager has no
  clean synchronous-CLI or single-round-trip-MCP shape) still holds; this
  release only fixes the import-path gap, which was a pure oversight, not
  a design question.
- **`tmux_kit/proc.py`'s `tmux_env()` docstring did not disclose that its
  `env.pop("TMUX", None)` line is load-bearing**, not incidental cleanup:
  it is the entire reason this module's production `run_tmux()` call
  path is safe against the `$TMUX`-outranks-`TMUX_TMPDIR` hazard without
  requiring an explicit `-L`/`-S` on every call the way
  `tmux_kit.isolation` does. The docstring now says so explicitly, names
  what breaks if the pop is ever made conditional, and cross-references
  `tmux_kit/isolation.py`. Also explicitly decided and written down (not
  left ambiguous): `tests/test_rails.py`'s `-L`/`-S` CI rail continues to
  exclude `tmux_kit/`'s own production contract by name -- this is a
  DECIDED position (proc.py never merely relies on `TMUX_TMPDIR`
  outranking `$TMUX`; it removes `$TMUX` from the child environment
  entirely, so there's no environment shape in which the hazard applies),
  not an oversight, and both files now say so.
- **Phantom console script.** `README.md` and `tmux_kit/mcp_server.py`'s
  own module docstring (twice) told a reader to run `tmux-kit-mcp`, but
  `pyproject.toml`'s `[project.scripts]` registered only `tmux-kit` --
  a reader following either verbatim got `command not found`. Fixed by
  adding the `tmux-kit-mcp = "tmux_kit.mcp_server:main"` entry point
  (functional once the `mcp` extra is installed, same guarded-import
  pattern as the existing `tmux-kit` script), making the existing
  documentation true rather than rewriting it to describe a script that
  didn't exist.
- **Stale version pin in README.md.** Lines 127/131 pinned
  `tmux-kit==0.2.0` / `@v0.2.0` against an actual version of 0.3.1 --
  updated to the version this release ships as (0.3.2).
- **False cross-surface naming-parity claim in README.md.** The text
  claimed "same names in the CLI, the MCP tool descriptions, and
  `tmux_kit.api`," which is not true: `is_running` has neither a CLI
  command nor an MCP tool (it's a convenience wrapper around `status`),
  and the CLI shortens two verbs for terminal ergonomics
  (`list_sessions` -> `list`, `wait_for_attention` -> `wait`) that MCP
  keeps at full length -- the README's own next code block
  (`tmux-kit list --json`) contradicted the claim three lines above it.
  Fixed by stating the actual mapping honestly instead of renaming the
  CLI's already-tested, already-shipped short verbs to force a literal
  match -- that would be a user-facing breaking change to a stable
  surface for a documentation claim, not a documentation fix.

### Added

- **`observe.pane_exit_code()` / `api.exit_code()` / CLI `exit-code` /
  MCP `exit_code`** -- "did it SUCCEED?" for a finished session, via
  tmux's own `#{pane_dead_status}`. `status()` deliberately only answers
  running/finished/missing, never success/failure -- every persona in
  the simulated user research asked some form of "did the build pass?",
  a question this library could already half-answer (tmux exposes the
  fact) but had no entry point for. Purely additive: `status()`'s
  existing three-value contract is completely unchanged, so nothing that
  depends on it (CLI/MCP docs, existing tests, other consumers) is
  affected. Same "unknown, not a fact" convention as `pane_is_dead()`:
  returns `None` (never raises) when the pane is still running, the
  session is gone, tmux is unreachable, or the exit status was never
  retained (tmux's factory-default `remain-on-exit off` tears a dead
  pane down immediately, so the common case is simply "nothing left to
  ask" -- the caller needs `remain-on-exit on` for a durable answer).
- `wait_for_attention`'s bell-flag stickiness (reading the flag does not
  clear it -- an agent polling in a loop can get `True` forever from a
  stale bell) is now stated in the MCP tool description, the CLI `--help`
  text, and the underlying library docstrings already carried it
  (`bell.poll_bell_flag()`/`wait_for_bell()`) but neither surface's own
  docs repeated the warning where an agent would actually see it.

### Changed

- **MCP `wait_for_attention`'s default `timeout` is now 30 seconds**
  (was `None`, wait forever). An MCP tool call is a single request/
  response round trip made by an unsupervised agent with no Ctrl-C --
  unlike a human-invoked CLI command (`tmux-kit wait`, unchanged, still
  defaults to waiting forever, which is the correct default for an
  interactive invocation a human can interrupt) or a deliberate direct
  library call (`tmux_kit.api.wait_for_attention()` /
  `tmux_kit.bell.wait_for_bell()`, both unchanged). Waiting forever
  remains available at the MCP surface too -- pass `timeout=None`
  explicitly -- but it is now an opt-in, not the default a caller falls
  into by omission.

### Considered, not built

- **A bell-flag "acknowledge/clear" mechanism**, so a caller could
  positively clear a stale `True` after acting on it, instead of relying
  on re-reading `read`/`search` to distinguish a new bell from a stale
  one. Deferred: tmux has no first-class "clear this window's bell flag"
  command short of switching the client's attached window (not
  applicable to a detached, agent-managed session) -- a real ack
  mechanism needs its own design (what actually clears the tmux-native
  flag, keyed by what) and its own tests, not a bolt-on under this
  release's time budget. Documented the stickiness hazard everywhere an
  agent will read it instead (see Added, above); worth its own
  follow-up.
- **Renaming the CLI's `list`/`wait` back to `list_sessions`/
  `wait_for_attention`** to literally satisfy the README's (now-corrected)
  parity claim. Rejected: these are shipped, tested command names on a
  library already at 0.3.x: the ergonomic case for a short CLI verb
  (typed by a human, in a shell) is real and different from an MCP tool
  name (chosen once, by a client library, never typed), and renaming a
  stable CLI surface to make a documentation sentence literally true is
  backward for a patch release. The honest-mapping fix (see Fixed,
  above) resolves the actual defect -- the false claim -- without an
  unrelated breaking change.

### Verified

- Full suite (`uv run pytest -v`), all extras installed (dev + cli +
  mcp): 234 passed. All incident/rail tests (presence, cgroup-escape,
  differential harness, `test_rails.py`'s run-shell/import-purity/
  isolation rails) unchanged and green -- none of the fixes above touch
  their assertions.

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
