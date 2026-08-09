# tmux-kit — Conventions for Agents & Contributors

This is a small, standalone public library (`bkrabach/tmux-kit`, PyPI `tmux-kit`,
import `tmux_kit`), extracted from muxplex so a second application can manage
tmux sessions without forking that code. See `README.md` for the module map
and `tmux_kit/CONSUMERS.md` for the dependency contract and public surface.
This file is the shorter version: what an agent must not break.

## `dependencies = []` in `pyproject.toml` is load-bearing, not an oversight

This library is **stdlib-only by contract**. `tests/test_import_smoke.py`'s
`test_lib_distribution_declares_zero_dependencies` and
`test_public_surface_imports_without_the_muxplex_server` enforce this at
both the metadata level and the actual-import level. Do not add a
dependency — even a small, "obviously fine" one — without treating it as a
breaking change to the library's whole reason to exist: a second consumer
depends on `tmux-kit` specifically *because* it does not drag in fastapi,
httpx, pam, or any of muxplex's server stack. If a feature seems to need a
third-party package, that is a signal to solve it with stdlib, or to leave
it out of the library (see CONSUMERS.md's "NOT in the library yet" list —
several things are deliberately deferred to the consumer for exactly this
reason), not to add the dependency.

Config is **injected, never read**: no module here knows a settings file
exists. `tmux_kit.proc.set_env_factory()` is how a consumer supplies its own
socket-dir resolution; don't reach for `os.environ` reads scattered through
the library as a shortcut.

## The two safety rails (`tests/test_rails.py`) — do not weaken either

Both map to real production incidents (see muxplex's own `AGENTS.md` for the
full incident writeups — this library carries the code-level guards, that
repo carries the incident narratives):

- **Never-render / one legal `run-shell` site.**
  `test_exactly_one_run_shell_construction_site_exists` asserts there is
  exactly ONE place in this codebase that builds a tmux `run-shell` command
  (`tmux_kit/bell.py`'s `build_alert_bell_hook()`), and that it is
  structurally incapable of a "loud" variant (no `loud`/`verbose`/`silent`
  parameter — a tmux background command's stdout/stderr gets painted onto a
  live client's active pane unless the hook is deliberately silent). This
  guards against the incident where a diagnostic `run-shell` spammed curl
  errors across 53 live sessions. Never add a second `run-shell` construction
  site; extend `build_alert_bell_hook()`'s single call site instead.
- **Import purity.** `test_library_is_import_pure_stdlib_and_self_only` AST-
  scans every CORE module under `tmux_kit/` (everything except the optional
  `cli.py`/`mcp_server.py` extras -- see below) and fails if anything
  imports outside stdlib + `tmux_kit.*`. This is the mechanical enforcement
  of the `dependencies = []` contract above — it catches an import that a
  metadata check alone would miss (e.g. an accidental `import requests`
  that happens to be present in the dev environment).
- **Optional extras (0.2.0): `tmux_kit/cli.py` (Click) and
  `tmux_kit/mcp_server.py` (the `mcp` SDK).** Each depends on exactly one
  third-party package, declared in `pyproject.toml`'s `cli`/`mcp` extras,
  and is scoped OUT of the core import-purity scan above -- but each has
  its OWN, narrower rail (`test_cli_extra_imports_only_click_stdlib_and_tmux_kit`,
  `test_mcp_extra_imports_only_mcp_stdlib_and_tmux_kit`) asserting it
  imports nothing beyond stdlib + `tmux_kit.*` + its one named package.
  Adding a THIRD extra means adding it to `test_rails.py`'s `_EXTRA_MODULES`
  with its own scoped rail -- never widening the core scan or an existing
  extra's allowed import to cover it.
- `test_no_test_modules_inside_the_library_package` keeps tests out of the
  shipped package (`tmux_kit/`) so a consumer's install stays lean.

If a change makes one of these fail, the fix is almost never to weaken the
assertion — it is to find a different way to implement the feature that
respects the boundary the rail protects.

## `TMUX_TMPDIR` is not an isolation boundary — `$TMUX` wins unless you pass `-L`/`-S`

A real incident, 2026-08 (see CHANGELOG 0.2.1): an agent probing tmux's
`remain-on-exit` behavior set `TMUX_TMPDIR` to a fresh directory and believed
that isolated it from the operator's real tmux server. It did not. The
agent's shell was itself running inside a tmux pane, so `$TMUX` was already
set — and **tmux's socket resolution prefers an inherited `$TMUX` over
`TMUX_TMPDIR` whenever no explicit `-L`/`-S` flag is given.** The probe's own
`tmux list-sessions` printed 73 of the operator's real sessions; a
`tmux kill-server` two lines later destroyed all of them.

Verified against tmux 3.4 on the affected host:

```
$TMUX set, no -L:   TMUX_TMPDIR=/tmp/x tmux list-sessions      -> the 20+ REAL sessions
$TMUX set, WITH -L: tmux -L some-random-name list-sessions     -> "no such file" (correctly isolated)
```

**The rule an agent composing a shell command must internalize:** if you are
running inside a tmux pane (you almost always are, in this ecosystem) and
you invoke `tmux` for ANY reason — a probe, a one-off diagnostic, a
throwaway test — an explicit `-L <name>` or `-S <path>` is not optional
hygiene, it is the ONLY thing that actually redirects you away from the
ambient server. `TMUX_TMPDIR` alone, `env -u TMUX` alone, a "fresh tmp dir"
alone — none of these substitute for it if you skip the explicit flag.

**Do not hand-roll this.** Use `tmux_kit.isolation.isolated_tmux_server()` —
an async context manager that gives you a unique `-L` socket, a scrubbed
`$TMUX`, a private `TMUX_TMPDIR`, and guaranteed teardown (even on
exception), so there is nothing left to get wrong:

```python
from tmux_kit.isolation import isolated_tmux_server

async with isolated_tmux_server() as server:
    await server.run("new-session", "-d", "-s", "probe")
    out = await server.run("list-sessions")
# torn down here, even if the block raised
```

`tests/test_rails.py`'s `test_tests_and_examples_never_invoke_tmux_without_explicit_isolation`
enforces this structurally: it fails the build if any test, example, or
script anywhere in this repo (outside `tmux_kit/`'s own production contract)
spawns a real `tmux` subprocess without a literal `-L`/`-S` in that same
call — a library-only guard would have missed this incident entirely, since
the kill never went through `tmux_kit` at all; it was a hand-written shell
command.

## The presence record is POSITIVE — never a TTL, never a sweep

`tmux_kit.presence` tracks which tmux sessions are known-alive by **positive
observation only**: a session is removed from the manifest when its death is
individually confirmed against a live, identity-matched tmux server — never
by a timeout, an age check, or a periodic sweep. Adding a TTL or "clean up
anything not seen in N cycles" is exactly how 52 live sessions were lost in
production once (muxplex's incident history). `presence.update_manifest()`
also round-trips any unknown top-level manifest key verbatim (see
`tests/test_presence.py` and muxplex's `test_tmux_kit_contract.py`, which
pins this from the consumer side) — a consumer stores its own app-level
state alongside the library's keys in the same manifest; a "clean rebuild"
that drops unrecognized keys would silently destroy that state.

## Incident tests encode real production incidents — do not weaken them to pass

`tests/test_presence.py`, `tests/test_cgroup_escape.py`,
`tests/test_differential_harness.py`, and `tests/test_rails.py` are not
ordinary unit tests written against a spec — several assertions exist
*because a specific incident happened in production* (lost sessions,
cgroup-adoption kills, a painted pane, a multi-window bell misattribution).
When one of these fails after a change:

1. **First ask whether behavior actually changed**, and if so, whether that
   is the intended fix or a regression of the thing the test was written to
   prevent.
2. If the test's assertion no longer matches the code's real structure
   (e.g. a legitimate refactor), update the assertion to follow the new
   structure — **never loosen it to merely pass.** A weakened incident test
   is worse than the failure it silenced, because the next regression of
   the same incident goes undetected.
3. If you are unsure whether a test is incident-derived, check for a comment
   or docstring naming the incident before touching it — most do.

The differential harness (`pytest -m differential`) replays fleet-recorded
real-tmux data against the presence rule; the integration suite
(`pytest -m integration`) runs against a real, isolated (`-L` socket) tmux
server. Both must stay green, and both must run in CI unconditionally (a CI
runner has no live muxplex to endanger, unlike a contributor's dev box).

## Versioning is lockstep with muxplex — a bump is a coordinated two-repo release

muxplex pins `tmux-kit==X.Y.Z` **exactly** and separately carries a
`[tool.uv.sources] tmux-kit = { git = ..., tag = "vX.Y.Z" }` entry that must
name the identical version (see muxplex's `AGENTS.md`, "tmux-kit pin/tag
agreement"). This repo's own version in `pyproject.toml` and its git tag
(`vX.Y.Z`) must match each other, and the tag must exist before muxplex's
pin/source can point at it. A version bump here is never a solo release:

1. Bump `pyproject.toml`'s `version` here, land it, tag `vX.Y.Z`, publish to
   PyPI.
2. Only then bump muxplex's `tmux-kit==X.Y.Z` pin AND its
   `[tool.uv.sources]` `tag`, together, in the same muxplex commit, followed
   by `uv lock` there.

Publishing a new tmux-kit version that muxplex hasn't picked up yet is
harmless (muxplex just doesn't see it until its own pin bumps). The
dangerous direction is muxplex's pin and source tag disagreeing with each
other, or either one naming a tmux-kit version that was never actually
tagged/published here — verify the tag exists on this repo before it's
referenced from muxplex's side.

0.x semantics: no semver promise. Muxplex is the only consumer today, but
API changes should still be treated as consumer-facing — a second real
consumer is the intended audience for this repo existing at all (see
CONSUMERS.md's "NOT in the library yet" section for what's deliberately
left open for that consumer to shape).
