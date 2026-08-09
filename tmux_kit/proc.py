"""tmux subprocess plumbing: ``run_tmux()`` and ``tmux_env()``.

Moved verbatim from ``sessions.py`` at stage S1; stage S2 (plan §13.2
stage 3 -- docs/plans/2026-08-08-tmux-lib-extraction-plan.md) inverted the
one wrong-way import arrow this module carried: it used to read muxplex's
settings file (``load_settings()`` -- ``sessions.py:294`` pre-move). Per
plan §4.3, **configuration is injected, never read**: this module has no
idea any settings file exists.

Two injection points, both owned by the host application:

- ``tmux_env(socket_dir)`` is now a PURE function of its parameter (plus
  ``os.environ``): the caller resolves the socket directory from wherever
  its config lives and passes the value in.
- ``set_env_factory(factory)`` installs a process-wide callable the app
  registers once at construction time (muxplex does so in ``sessions.py``,
  its app-side facade). Every ``run_tmux()`` call that is not given an
  explicit ``env=`` resolves the subprocess environment from that factory
  AT CALL TIME -- preserving the pre-S2 semantics exactly, where every
  tmux invocation re-read the setting fresh. With no factory installed
  (a bare library consumer that wants the ambient environment, or a unit
  test), the default is ``None`` -- inherit the process environment
  unchanged, which is also what the pre-S2 code did when
  ``tmux_socket_dir`` was unset.

Every tmux invocation in the package goes through ``run_tmux()``; this
module is the one door (plan §1.2).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

# The app-installed environment factory (plan §4.3). ``None`` means "no
# override installed": subprocesses inherit this process's environment,
# byte-identical to the pre-S2 behavior with `tmux_socket_dir` unset.
_env_factory: Callable[[], dict[str, str] | None] | None = None

# Sentinel distinguishing "caller passed no env" from the meaningful
# ``env=None`` ("inherit the ambient environment, explicitly"). Exported
# (as ``UNSET``) so every function in this package that takes an ``env=``
# parameter can share ONE "did the caller actually pass something" marker
# -- see ``spawn.spawn_session()``'s incident-derived fix note for why a
# second, look-alike sentinel almost shipped a real footgun.
_UNSET: object = object()
UNSET = _UNSET


def set_env_factory(factory: Callable[[], dict[str, str] | None] | None) -> None:
    """Install the host application's subprocess-environment factory.

    The factory is called on EVERY ``run_tmux()`` invocation (and, since
    the fix below, every ``spawn_session()`` call) that does not pass an
    explicit ``env=``, so config re-resolution stays per-call (the
    pre-inversion cadence -- a settings edit takes effect on the next tmux
    call, no restart required). muxplex installs
    ``tmux_env(<socket dir from its settings>)`` here; a second app
    installs its own. Pass ``None`` to uninstall.
    """
    global _env_factory
    _env_factory = factory


def get_env_factory() -> Callable[[], dict[str, str] | None] | None:
    """Return the currently-installed env factory, or ``None`` if none is
    installed.

    Exists so a higher layer (``tmux_kit.api``'s facade) can ask "has a
    consumer already wired their own environment resolution?" before
    installing a default, WITHOUT reaching into this module's private
    ``_env_factory`` global directly -- see ``api._ensure_wired()``.
    """
    return _env_factory


def default_env() -> dict[str, str] | None:
    """Resolve the subprocess environment from the injected factory.

    Returns ``None`` (inherit this process's environment unchanged) when no
    factory is installed.
    """
    return _env_factory() if _env_factory is not None else None


def tmux_env(socket_dir: str | None) -> dict[str, str] | None:
    """Build the environment for tmux subprocess calls, honoring *socket_dir*.

    S2 inversion (plan §4.3, §7.1): the socket directory is PASSED IN by
    the caller -- this function no longer reads any configuration. muxplex
    resolves its ``tmux_socket_dir`` setting app-side and injects it here.

    Why the override exists at all: a systemd/launchd service does NOT
    inherit the user's interactive login shell environment. If the user
    sets TMUX_TMPDIR in their shell rc (common when keeping sockets out of
    the shared, world-writable /tmp), the muxplex *service* process never
    sees it -- tmux silently falls back to its compiled-in default
    (/tmp/tmux-$UID) and every real session becomes invisible to muxplex,
    even though `tmux list-sessions` works fine when run interactively by
    the same user.

    Returns:
        None if *socket_dir* is unset/empty -- callers should pass
        `env=None` to the subprocess call, inheriting the process's own
        environment unchanged (fully backward compatible).
        Otherwise, a copy of `os.environ` with `TMUX_TMPDIR` overridden to
        the given directory. Copying (not replacing) preserves PATH,
        HOME, and everything else the subprocess needs.

        **`env.pop("TMUX", None)` below is LOAD-BEARING, not defensive
        polish -- read this before touching it.** tmux gives an inherited
        `$TMUX` (set whenever a process is a descendant of an *attached*
        tmux client) priority over `TMUX_TMPDIR` when resolving which
        server socket to talk to. If this line were removed or made
        conditional, a caller of `run_tmux()` that happens to be a
        descendant of some other tmux client (e.g. this process was
        started manually from inside a tmux pane while debugging, or --
        the exact incident class this matters for -- an agent inside a
        tmux pane invoking this library) would silently ignore this
        function's `TMUX_TMPDIR` override and keep talking to THAT other,
        ambient server instead -- byte-for-byte the mechanism that
        destroyed 73 of an operator's live sessions in a real incident
        (see AGENTS.md's "`TMUX_TMPDIR` is not an isolation boundary", and
        `tmux_kit/isolation.py`'s module docstring for the verified
        `-S` > `-L` > `$TMUX` > `TMUX_TMPDIR` > compiled-in-default
        precedence chain). Unconditionally removing `TMUX` here -- so the
        subprocess never sees it at all, rather than merely hoping
        `TMUX_TMPDIR` outranks it -- is what makes this override actually
        safe: there is no environment shape in which `$TMUX` can leak
        through and win.

        This is also *why* this module's `run_tmux()` is deliberately NOT
        required to pass an explicit `-L`/`-S` flag the way
        `tmux_kit.isolation.isolated_tmux_server()` is (see
        `tests/test_rails.py`'s isolation rail, which excludes this
        package's own production contract by name -- that exclusion is a
        DECIDED position, not an oversight): `-L`/`-S` is the fix for a
        caller that only sets `TMUX_TMPDIR` and leaves `$TMUX` in place
        (the actual incident, hand-rolled outside this library); this
        function never leaves `$TMUX` in place to begin with, so the
        hazard the rail guards against does not apply to this call site.
        If a future change ever makes this pop conditional (e.g. "only
        strip TMUX if X"), that reasoning breaks, and the CI rail's
        exclusion of `tmux_kit/` stops being correct -- revisit both
        together, not just one.

        The muxplex *service* itself is never an attached tmux client
        under normal (systemd/launchd) deployment, so removing `TMUX` is a
        no-op there -- this matters specifically for atypical invocation
        contexts (interactive debugging, or any consumer's process that
        might itself run inside a tmux pane), which is exactly the
        scenario a caller cannot rule out in general.
    """
    if not socket_dir:
        return None
    env = dict(os.environ)
    env["TMUX_TMPDIR"] = socket_dir
    env.pop("TMUX", None)  # load-bearing -- see this function's docstring
    return env


async def run_tmux(*args: str, env: dict[str, str] | None | object = _UNSET) -> str:
    """Run `tmux <args>` in a subprocess and return stdout as a string.

    The subprocess environment is INJECTED config (plan §4.3): pass it
    explicitly via ``env=`` (e.g. the result of ``tmux_env(socket_dir)``),
    or omit it to use the app-installed factory (``set_env_factory``),
    which is how the host's socket-dir override reaches every tmux call
    made from inside the library. ``env=None`` explicitly inherits this
    process's environment.

    Raises:
        RuntimeError: If the process exits with a nonzero return code.
                      The error message contains the decoded stderr output.
    """
    if env is _UNSET:
        env = default_env()
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,  # type: ignore[arg-type]
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr_bytes.decode("utf-8", errors="replace"))
    return stdout_bytes.decode("utf-8", errors="replace")
