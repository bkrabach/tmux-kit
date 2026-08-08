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

0.2.0 update: the import-purity rail is now scoped to the CORE primitives
(``proc``/``spawn``/``names``/``observe``/``presence``/``bell``/``keys``/
``cgroup``/``lifecycle``/``api``/``__init__``) -- ``tmux_kit/cli.py`` and
``tmux_kit/mcp_server.py`` are optional EXTRAS (pyproject.toml's `cli`/
`mcp` extras) that legitimately depend on ``click``/``mcp`` respectively.
This is a NARROWING of scope, not a weakening of protection: each extra
gets its OWN rail immediately below, asserting it imports nothing beyond
stdlib + ``tmux_kit.*`` + its one named third-party package. The
stdlib-only, zero-runtime-dep contract for the BASE package (`import
tmux_kit`) is exactly as strict as before -- see AGENTS.md's "dependencies
= [] is load-bearing" section.

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


# Modules in this package that are OPTIONAL EXTRAS, not part of the
# stdlib-only base package (see pyproject.toml's `cli`/`mcp` extras and
# AGENTS.md). Each is scoped OUT of the core import-purity scan below and
# given its OWN, narrower rail immediately after it.
_EXTRA_MODULES: dict[str, str] = {
    "cli.py": "click",
    "mcp_server.py": "mcp",
}


def _core_python_files() -> list[Path]:
    """Every ``.py`` under ``tmux_kit/`` EXCEPT the optional-extra modules
    in ``_EXTRA_MODULES`` (which get their own, separately-scoped rail).
    """
    return [p for p in sorted(_LIB_DIR.rglob("*.py")) if p.name not in _EXTRA_MODULES]


def _import_purity_offenders(
    paths: list[Path] | None = None, *, extra_allowed: str | None = None
) -> list[str]:
    """AST scan of *paths* (default: the core modules -- see
    ``_core_python_files()``) for imports that reach outside stdlib +
    ``tmux_kit.*`` (+ *extra_allowed*, one additional permitted top-level
    package, for an extras-scoped call).

    Three shapes are offenses:
    - ``from <disallowed> import ...`` (absolute ImportFrom)
    - ``import <disallowed>`` (plain Import)
    - a RELATIVE import whose level climbs OUT of the ``tmux_kit/`` package
    """
    offenders: list[str] = []
    for path in paths if paths is not None else _core_python_files():
        rel_parts = path.relative_to(_LIB_DIR).parts
        rel = "tmux_kit/" + "/".join(rel_parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    mod = node.module or ""
                    root = mod.split(".")[0]
                    if (
                        root != "tmux_kit"
                        and root not in _STDLIB_MODULES
                        and root != extra_allowed
                    ):
                        offenders.append(f"{rel}:{node.lineno}: from {mod} import ...")
                elif node.level >= len(rel_parts) + 1:
                    offenders.append(
                        f"{rel}:{node.lineno}: relative import escapes tmux_kit/ "
                        f"(level={node.level})"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if (
                        root != "tmux_kit"
                        and root not in _STDLIB_MODULES
                        and root != extra_allowed
                    ):
                        offenders.append(f"{rel}:{node.lineno}: import {alias.name}")
    return offenders


def test_library_is_import_pure_stdlib_and_self_only():
    """No CORE module under ``tmux_kit/`` imports anything outside stdlib +
    ``tmux_kit.*`` (plan §3.2 -- "the import-purity rail becomes
    tmux-kit's simplest test"; 0.2.0 -- scoped to exclude the optional
    ``cli``/``mcp`` extras, each of which has its own rail below).

    This is the AST-level counterpart to ``test_import_smoke.py``'s
    runtime check (a fresh-interpreter import that inspects
    ``sys.modules``); together they close the same erosion from two
    independent angles, exactly as muxplex's own pair does pre-split.
    """
    offenders = _import_purity_offenders()
    assert not offenders, (
        f"tmux_kit/'s core (stdlib-only by contract) imports something "
        f"outside stdlib + tmux_kit.*: {offenders}. This library must stay "
        f"importable by a consumer with ZERO extra runtime weight -- "
        f"config/deps are INJECTED by the caller, never read here. (If this "
        f"is a legitimate new optional-extra module, add it to "
        f"_EXTRA_MODULES with its own scoped rail -- do not weaken this one.)"
    )


def test_cli_extra_imports_only_click_stdlib_and_tmux_kit():
    """``tmux_kit/cli.py`` (the `cli` extra) may import stdlib +
    ``tmux_kit.*`` + ``click`` -- nothing else. This is the CLI's own
    narrow carve-out from the core import-purity rail above, not a
    loosening of it: the base package still drags in zero third-party
    weight, and the CLI extra is honest about the one dependency it adds.
    """
    path = _LIB_DIR / "cli.py"
    assert path.is_file(), f"expected {path} to exist"
    offenders = _import_purity_offenders([path], extra_allowed="click")
    assert not offenders, (
        f"tmux_kit/cli.py imports something outside stdlib + tmux_kit.* + "
        f"click: {offenders}. The `cli` extra's pyproject.toml dependency "
        f"list is `click` only -- an unlisted import here would install-fail "
        f"or silently work by dev-environment accident."
    )


def test_mcp_extra_imports_only_mcp_stdlib_and_tmux_kit():
    """``tmux_kit/mcp_server.py`` (the `mcp` extra) may import stdlib +
    ``tmux_kit.*`` + ``mcp`` -- nothing else. Same rationale as the CLI's
    rail immediately above.
    """
    path = _LIB_DIR / "mcp_server.py"
    assert path.is_file(), f"expected {path} to exist"
    offenders = _import_purity_offenders([path], extra_allowed="mcp")
    assert not offenders, (
        f"tmux_kit/mcp_server.py imports something outside stdlib + "
        f"tmux_kit.* + mcp: {offenders}. The `mcp` extra's pyproject.toml "
        f"dependency list is `mcp` only."
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
