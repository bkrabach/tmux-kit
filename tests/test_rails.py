"""Meta-tests: tmux-kit's own half of muxplex's two-rail safety scheme.

muxplex's ``AGENTS.md`` documents the incident these rails guard against
(tmux's ``run-shell`` painting a background command's output onto a live
client's active pane -- see "muxplex must never emit anything that
renders on a user's terminal"). Before the split, one shared scan
(``muxplex/tests/test_safety_rails.py``) covered both the ``muxplex``
app package and ``lib/tmux_kit/``. Post-split (plan §3.2), each repo
keeps its own half, scoped to what it actually owns:

- muxplex tightens to ZERO ``run-shell`` construction sites anywhere in
  its own tree (plan §3.3) -- that rail lives in muxplex's own suite.
- tmux-kit (here) keeps the ONE legal construction site
  (``tmux_kit/bell.py``'s ``build_alert_bell_hook()``) and the
  import-purity rail (no module under ``tmux_kit/`` may import anything
  outside stdlib + ``tmux_kit.*`` -- plan §3.2's "simplest test").

If you are here because one of these failed: the rail is not ceremony --
each one maps to real production damage that already happened once
(see muxplex's AGENTS.md, "never render to a pane" / the 2026-07 curl
run-shell incident).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_LIB_DIR = _REPO_ROOT / "tmux_kit"

# Standard library module names this library may import. Anything a
# module under tmux_kit/ imports that is NOT in this set and NOT
# `tmux_kit` itself is an import-purity violation (plan §3.2: "no module
# under tmux_kit/ imports anything outside stdlib + tmux_kit.*").
_STDLIB_MODULES = set(sys.stdlib_module_names) | {"__future__"}


def _run_shell_construction_sites() -> list[str]:
    """Recursive structural scan of tmux_kit/ for `run-shell` construction
    sites, returned as repo-relative offender strings.

    Deliberately `.startswith()`, not a bare substring match: docstrings
    and comments legitimately discuss `run-shell` in prose without ever
    constructing one, and a substring match would flag those as false
    positives (see muxplex's `test_safety_rails.py`, which this mirrors).
    """
    assert _LIB_DIR.is_dir(), f"run-shell rail scan root missing: {_LIB_DIR}"
    offenders: list[str] = []
    for path in sorted(_LIB_DIR.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.strip().startswith("run-shell")
            ):
                offenders.append(f"{rel}:{node.lineno}: {node.value!r}")
    return offenders


def test_exactly_one_run_shell_construction_site_exists():
    """Exactly ONE place in this library may ever build a `run-shell`
    command string, and it must be `build_alert_bell_hook()`
    (tmux_kit/bell.py) -- never a diagnostic, probe, or health-check call.
    """
    offenders = _run_shell_construction_sites()
    assert len(offenders) == 1, (
        f"Expected exactly ONE `run-shell` construction site (the library's "
        f"build_alert_bell_hook), found {len(offenders)}: {offenders}. tmux's "
        f"`run-shell` paints a background command's output onto a live "
        f"client's active pane -- see muxplex's AGENTS.md 'never render to a "
        f"pane' rule. A second construction site is almost certainly a new "
        f"diagnostic/probe and must be rejected outright, not silenced."
    )
    assert offenders[0].startswith("tmux_kit/bell.py"), (
        f"the sole run-shell construction site moved out of tmux_kit/bell.py: "
        f"{offenders[0]!r} -- verify this is still build_alert_bell_hook() "
        f"(the one API that wraps a caller-supplied, always-silent command)."
    )


def _import_purity_offenders() -> list[str]:
    """AST scan of every ``.py`` under ``tmux_kit/`` for imports that
    reach outside stdlib + ``tmux_kit.*``.

    Three shapes are offenses:
    - ``from <non-stdlib, non-tmux_kit> import ...`` (absolute ImportFrom)
    - ``import <non-stdlib, non-tmux_kit>`` (plain Import)
    - a RELATIVE import whose level climbs OUT of the ``tmux_kit/`` package
    """
    offenders: list[str] = []
    for path in sorted(_LIB_DIR.rglob("*.py")):
        rel_parts = path.relative_to(_LIB_DIR).parts
        rel = "tmux_kit/" + "/".join(rel_parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    mod = node.module or ""
                    root = mod.split(".")[0]
                    if root != "tmux_kit" and root not in _STDLIB_MODULES:
                        offenders.append(f"{rel}:{node.lineno}: from {mod} import ...")
                elif node.level >= len(rel_parts) + 1:
                    offenders.append(
                        f"{rel}:{node.lineno}: relative import escapes tmux_kit/ "
                        f"(level={node.level})"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root != "tmux_kit" and root not in _STDLIB_MODULES:
                        offenders.append(f"{rel}:{node.lineno}: import {alias.name}")
    return offenders


def test_library_is_import_pure_stdlib_and_self_only():
    """No module under ``tmux_kit/`` imports anything outside stdlib +
    ``tmux_kit.*`` (plan §3.2 -- "the import-purity rail becomes
    tmux-kit's simplest test").

    This is the AST-level counterpart to ``test_import_smoke.py``'s
    runtime check (a fresh-interpreter import that inspects
    ``sys.modules``); together they close the same erosion from two
    independent angles, exactly as muxplex's own pair does pre-split.
    """
    offenders = _import_purity_offenders()
    assert not offenders, (
        f"tmux_kit/ (stdlib-only by contract) imports something outside "
        f"stdlib + tmux_kit.*: {offenders}. This library must stay importable "
        f"by a consumer with ZERO extra runtime weight -- config/deps are "
        f"INJECTED by the caller, never read here."
    )


def test_no_test_modules_inside_the_library_package():
    """Test files must live in this repo's own tests/ directory, never
    inside tmux_kit/ itself -- a stray test module there would silently
    escape this suite's autouse safety rails (isolated TMUX_TMPDIR).
    """
    strays = sorted(
        p.relative_to(_LIB_DIR).as_posix()
        for p in _LIB_DIR.rglob("*.py")
        if p.name.startswith("test_") or p.name == "conftest.py"
    )
    assert not strays, f"Test files found inside tmux_kit/: {strays}."
