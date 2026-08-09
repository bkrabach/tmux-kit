# Building on `tmux-kit`

`tmux-kit` is a reusable tmux session-management library extracted from muxplex so
other apps can manage tmux sessions the way muxplex does — **without forking that
code**. Fixes flow both ways: a change to `tmux-kit` reaches every consumer.

> **Status:** `tmux-kit` now lives in its own public repo (`bkrabach/tmux-kit`),
> held at **0.x with no semver promise**. This file travelled with the package
> from the muxplex monorepo to this, its new home.
>
> **Naming:** the PyPI distribution and repo are `tmux-kit` (hyphen); the Python
> import package is `tmux_kit` (underscore) — hyphens are illegal in Python
> identifiers, so `pip install tmux-kit` → `import tmux_kit` is the standard
> arrangement (cf. `python-dateutil` → `dateutil`).

## How to depend on it

**Public installs (primary path, resolves from PyPI):**
```toml
dependencies = ["tmux-kit==0.1.0"]      # applications pin exact; 0.x has no semver promise
```

**Pinned git install** (managed/locked environments that cannot reach public
PyPI — see muxplex's own `docs/plans/2026-08-09-tmuxkit-own-repo-and-pypi-plan.md`
§2.3 for the three-shape install runbook this pattern comes from):
```toml
dependencies = [
  "tmux-kit @ git+https://github.com/bkrabach/tmux-kit.git@v0.1.0",
]
```

**Never copy `tmux_kit` source into your app.** A byte-similar copy IS the drift
incident the extraction exists to prevent. Need a change? Open a PR against the
`tmux-kit` repo — never a fork.

## The public surface (as shipped in 0.2.0) -- THE canonical enumeration

Stdlib-only (this table). Importing `tmux_kit` pulls in NO web server, no
fastapi, no pam (enforced by a smoke test). This table is the ONE
hand-maintained enumeration of every module's exports -- the README used
to carry a second, independent copy; that second copy drifted (it named a
`names.rename_session` that never existed) and shipped a first-run
`ImportError` to a reader who copied it. Don't re-add a second copy; point
at this one instead.

| Module | What it gives you |
|--------|-------------------|
| `tmux_kit.proc` | `run_tmux()`, `tmux_env(socket_dir)`, `set_env_factory()`, `get_env_factory()`, `default_env()`, `UNSET` (the shared omitted-arg sentinel). **Config is injected, never read** — you install an env factory at startup; the lib never reads your settings file. |
| `tmux_kit.observe` | `enumerate_sessions()`, `probe_tmux_epoch()`, `capture_pane()` / `_metadata` / `_window`, `pane_is_dead()` (0.2.0 — "is it done, or still going?"), session caches + getters (`get_session_cwds()`, ...), `snapshot_all()`. Scrollback paging via absolute-line params (`capture_pane_metadata` + `capture_pane_window`). |
| `tmux_kit.names` | `is_valid_session_name()`, `is_tmux_stable_name()`, `rename_tmux_session()`, `SESSION_NAME_RE`. |
| `tmux_kit.presence` | `update_manifest()`, `compute_restore_plan()`, `mark_restored()`. Owns the core presence keys; **your app writes its own keys beside them, in its own state dir** — unknown top-level keys round-trip verbatim (contract-tested). |
| `tmux_kit.bell` | `poll_bell_flag()`, `wait_for_bell()` (0.2.0 — blocks until a bell rings, doesn't just poll once), `build_alert_bell_hook()`. |
| `tmux_kit.keys` | send-input argv builders + the permission fence (`input_allowed_for_session()`, `session_matches_allowlist()`, `redact_preview()`, `build_send_text_argv()`, `build_send_key_argv()`). |
| `tmux_kit.spawn` | `spawn_session(name, template, *, env)` — caller resolves the template. `env` omitted now consults the installed factory (0.2.0 fix — see CHANGELOG; previously silently ignored it). |
| `tmux_kit.lifecycle` | (0.2.0, new) `kill_session()`, `interrupt_session()` — spawn's missing counterpart; previously a consumer had to drop to `run_tmux("kill-session", ...)` by hand. |
| `tmux_kit.cgroup` | `should_escape()`, `wrap_exec_argv()`, `wrap_shell_argv()`, `environment_mode()`, `reset_probe_cache_for_tests()` — the systemd `--scope` escape that keeps sessions alive past the launching unit. |
| `tmux_kit.api` | (0.2.0, new) the FACADE — `start`, `list_sessions`, `status`, `is_running`, `read`, `page`, `search`, `wait_for_attention`, `stop`, `kill`, `rename`, `doctor`, `configure`, `default_socket_dir`. Re-exported at the top level (`import tmux_kit; tmux_kit.start(...)`). See README's Quickstart and this module's own docstring. |
| `tmux_kit.isolation` | (0.2.1, new) `isolated_tmux_server()` — an async context manager yielding an `IsolatedTmuxServer` (`.run(*args)`) bound to a unique, throwaway `-L` socket with `$TMUX` scrubbed and its own private `TMUX_TMPDIR`, torn down (kill-server + directory removal) even if the block raises. THE tool to reach for whenever a test, example, script, or agent needs to poke real tmux behavior without any chance of touching an ambient/production server — see its module docstring for the incident that motivated it and the exact `$TMUX`-vs-`-L`/`-S` precedence mechanism. |

**Optional extras (each its own `pyproject.toml` extra, NOT part of the
stdlib-only base package):**

| Module | Extra | What it gives you |
|--------|-------|--------------------|
| `tmux_kit.cli` | `cli` | A Click CLI (`tmux-kit` console script) over the exact `tmux_kit.api` verbs. |
| `tmux_kit.mcp_server` | `mcp` | An MCP (stdio) server over the exact same verbs, for agent callers. |

## NOT in the library yet — deferred until a consumer needs them

These are open **on purpose**. The library holds at 0.x precisely so the second
real consumer shapes them. If you need one, that's the signal to move it — say so.

- **`Sender` / `SendPolicy`** typed send-API. The argv builders + fence exist in
  `tmux_kit.keys`, and `tmux_kit.lifecycle` now has raw execute-side primitives
  (`kill_session`, `interrupt_session`) alongside them — but the deny-by-default
  POLICY object (which sessions may be typed into / killed, under what rule) is
  still unbuilt. Every 0.2.0 addition in this area is a raw, unguarded primitive,
  same trust model as `proc.run_tmux()` itself.
- **ttyd / embedded-terminal lifecycle.** muxplex still owns it; the seam is
  defined (`§16` of the extraction plan) but not cut. It is gated on YOUR embedded
  human-UX design — how you want people to reach a session is the forcing function.
- **manifest file I/O with injected paths.** The presence *logic* is in the lib;
  the file read/write defaults still assume muxplex's dir.

## Hazards to design around (each learned from a real incident)

1. **Never render to a live pane.** The lib enforces exactly one `run-shell`
   construction site (a bell hook that writes nothing to the user's terminal). A
   diagnostic that painted a pane once spammed 53 live sessions.
2. **Two apps, one tmux server = silent failures.** The tmux `alert-bell` hook is
   a single GLOBAL slot — last writer wins, the loser's bells go dark with no
   error. And presence cross-talk: your app's cold start can offer to restore
   another app's sessions. Scope your observation; don't stomp the hook.
3. **ttyd's socket fence.** If/when you take the terminal path: the lib hands you a
   live AF_UNIX socket that structurally cannot become a TCP listener. Keep it that
   way — ttyd silently falls back to an unauthenticated `INADDR_ANY:7681` writable
   terminal on the LAN otherwise.
4. **The presence record is a POSITIVE record** — removed only by observed
   individual session death against a live, identity-matched server. Never a TTL,
   never a sweep. Adding one is how 52 sessions got lost.

## An environment gotcha that will bite you

`tmux_kit.cgroup.should_escape()` returns True only where there is a usable systemd
`--user` session (`XDG_RUNTIME_DIR` + working `systemd-run --user`). A container
without one silently takes a different spawn path, so a test env without systemd
`--user` will NOT exercise the scope-escape branch. If you test in a container,
know that branch is dead there and prove it separately.

## References

- **Reference implementation:** muxplex itself — read how it installs the env
  factory, sets its state dir, and drives restore.
- **Design + rationale + the deferred-surface decisions:**
  `muxplex/docs/plans/2026-08-08-tmux-lib-extraction-plan.md` (§13–17 cover the
  second-consumer case); the rename to `tmux-kit` is
  `muxplex/docs/plans/2026-08-09-tmuxkit-own-repo-and-pypi-plan.md`.
- **Presence-rule regression bed:** `pytest -m differential` — reuse it if you
  ever touch `tmux_kit`.
