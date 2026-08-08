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

**stdlib only.** No fastapi, no httpx, no server code. Configuration is
injected, never read (§4.3): no function in this package knows that a
settings file exists.

## Modules

| Module | Provides |
|---|---|
| `tmux_kit.proc` | `run_tmux()`, `tmux_env(socket_dir)` — argv+env plumbing, `TmuxError` carrying tmux's stderr |
| `tmux_kit.spawn` | `spawn_session(name, resolved_template, env=...)` — cgroup-escaped, with the exists-after-nonzero-exit (TTY-attach) tolerance |
| `tmux_kit.names` | `SESSION_NAME_RE`, `is_valid_session_name`, `is_tmux_stable_name`, `rename_session` — security-load-bearing name validation |
| `tmux_kit.observe` | `probe_tmux_epoch`, `enumerate_sessions`, pane capture, snapshot caches |
| `tmux_kit.presence` | the manifest presence rule — pure functions, no I/O; unknown top-level keys round-trip verbatim (§13.3) |
| `tmux_kit.bell` | bell *detection* (`poll_bell_flag`) + `build_alert_bell_hook()` — the sole legal `run-shell` construction site, always silent |
| `tmux_kit.keys` | typed-input argv builders and the allowlist fence mechanism |
| `tmux_kit.cgroup` | `should_escape`, `wrap_exec_argv`, `wrap_shell_argv` — the 44-session systemd cgroup incident, packaged |

## Sharing one tmux server between two apps

The tmux server is a shared singleton and some of its state is a single
global slot. Before shipping a second consumer on a host that also runs
muxplex, read plan §17 — the `alert-bell` hook slot (last writer wins,
silently), presence cross-talk (scope your observations or your cold start
freezes the *other* app's sessions into your restore plan), fence overlap,
and session-name collisions.

## Versioning

0.x semantics — no semver promise yet. First release is `0.1.0` (the
0.44.0 numbering used inside the muxplex monorepo was a pin-repair
artifact that the rename to `tmux-kit` voided; see
`docs/plans/2026-08-09-tmuxkit-own-repo-and-pypi-plan.md` §4 in the
muxplex repo for the full reasoning). Note the PyPI distribution name
uses a hyphen (`tmux-kit`) while the Python import package uses an
underscore (`tmux_kit`), because hyphens are illegal in Python
identifiers (cf. `python-dateutil` -> `dateutil`):

```toml
# Public installs (primary path):
dependencies = ["tmux-kit==0.1.0"]

# Pinned git install (e.g. a managed environment that cannot reach
# public PyPI -- see CONSUMERS.md):
#   tmux-kit @ git+https://github.com/bkrabach/tmux-kit@v0.1.0
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
(`pytest -m differential`, replayed against fleet-recorded real-tmux data)
and a real-tmux integration suite (`pytest -m integration`, isolated `-L`
socket). Run the full suite locally:

```
uv sync --extra dev
uv run pytest
```

CI (`.github/workflows/test.yml`) runs the full suite -- including
`-m integration` and `-m differential` unconditionally, since a CI runner
has no live muxplex to endanger -- on Python 3.11/3.12/3.13, Linux and
macOS.
