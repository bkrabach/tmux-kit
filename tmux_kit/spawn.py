"""Session spawning: the general half of "create a tmux session from a
shell-command template".

Split out of ``sessions.spawn_session_command()`` at stage S2 (plan §13.2
stage 3, §15.1 -- docs/plans/2026-08-08-tmux-lib-extraction-plan.md). The
original plan left the whole spawn path app-side because it read muxplex's
settings (``sessions.py:691`` pre-move); the S2 inversion makes the
TEMPLATE CALLER-RESOLVED, which frees the general half -- the PATH
pre-flight, the ``{name}`` substitution with ``shlex.quote()``
defense-in-depth, the cgroup escape (the 44-session incident, see
``tmux/cgroup.py``), the 30s long-lived-command tolerance, and the
exists-despite-nonzero-exit TTY-attach tolerance -- to live behind the
library boundary, where a second app creating sessions needs it and must
not rediscover it.

What deliberately stays app-side (plan §4.3: configuration is injected,
never read): resolving WHICH template to run. muxplex resolves its
``session_commands`` / ``new_session_template`` settings in
``sessions.spawn_session_command()`` and passes the resolved template
string in; a second app resolves its own config its own way.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil

from tmux_kit.cgroup import should_escape, wrap_shell_argv
from tmux_kit.observe import enumerate_sessions_strict
from tmux_kit.proc import UNSET, default_env

_log = logging.getLogger(__name__)

SPAWN_TIMEOUT_SECONDS = 30
# Killing a launcher does not close stdout/stderr held by its descendants.
# Do not let those inherited descriptors turn timeout cleanup into another
# unbounded wait; the requested tmux session is deliberately outside this
# cleanup scope and is checked below.
SPAWN_CLEANUP_TIMEOUT_SECONDS = 1


def _close_launcher_transport(proc: asyncio.subprocess.Process) -> None:
    """Close this process's pipe ends after a timed-out launcher is killed.

    A descendant can retain stdout/stderr after the launcher has exited. The
    subprocess transport owns our corresponding pipe ends; closing those ends
    releases the event-loop resources without signalling the descendant's
    process group (and therefore without touching a created tmux server).
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()


async def spawn_session(
    name: str, template: str, *, env: dict[str, str] | None | object = UNSET
) -> tuple[bool, str | None]:
    """Run *template* (with ``{name}`` substituted) to create a tmux session
    named *name*. Returns ``(ok, error)``.

    *template* is a CALLER-RESOLVED arbitrary user shell command with a
    ``{name}`` placeholder (e.g. ``tmux new-session -d -s {name}``, or a
    user's own ``amplifier-workspace {name}``), so this stays shell-based to
    preserve that feature. Injection is closed by two layers: (1) the
    caller's name validation/allowlist guarantees the name has no shell
    metacharacters; (2) ``shlex.quote()`` here is defense-in-depth in case
    that is ever loosened -- for an allowlist-valid name it's a no-op.
    Callers at an API boundary MUST validate the name first
    (``is_valid_session_name``) -- this function does not, so it stays
    usable from a plain CLI process with no HTTP framework in scope.

    *env* is INJECTED config (plan §4.3), with the SAME omit-vs-``None``
    semantics as ``proc.run_tmux()``: passing nothing consults the
    app-installed ``proc.set_env_factory()`` factory (if any) at call time;
    passing ``env=None`` explicitly inherits this process's environment
    unchanged; passing an explicit dict uses it verbatim. muxplex always
    passes its settings-resolved ``tmux_env()`` explicitly, so this default
    is a no-op for it either way.

    Fixed 0.2.0: before this release the default was a bare ``None``
    (silently "inherit ambient, ignore any installed factory"), which
    diverged from every other function in this package (``run_tmux``,
    and everything built on it) that consults the installed factory when
    ``env`` is omitted. A consumer who called ``set_env_factory()`` once at
    startup and then called ``spawn_session(name, template)`` with no
    ``env=`` got a session created against the AMBIENT default tmux socket,
    while a subsequent ``enumerate_sessions()`` (which does consult the
    factory) looked for it on the configured socket and found nothing --
    the exact failure that motivated this fix (see CHANGELOG).

    The command may start a brand-new tmux SERVER (both the default template
    and e.g. ``amplifier-workspace`` start one if none is running yet). If we
    are running under a systemd --user unit, that server must NOT be spawned
    as a plain child of this process -- see ``lib/tmux_kit/cgroup.py``'s
    module docstring and AGENTS.md's "Two ways to destroy every live tmux
    session on this host" (mechanism #1).

    Some session commands (e.g. ``amplifier-workspace``) create the tmux
    session and then attempt to *attach* to it, which requires a TTY. When
    launched with no TTY available (a service process, or a non-interactive
    CLI invocation) the attach step fails with a non-zero exit code even
    though the session was successfully created. To handle this, when the
    command exits non-zero we check whether a tmux session with the
    requested name now exists -- if it does, we treat it as a success.

    Returns:
        (True, None) on success.
        (False, <error message>) on failure -- the caller decides how to
        surface it (HTTPException for an API, a printed FAIL line for a
        CLI).
    """
    if env is UNSET:
        env = default_env()

    # Pre-flight: check that the base command is on PATH.
    base_cmd = template.split()[0] if template.strip() else ""
    if base_cmd and not shutil.which(base_cmd):
        _log.error(
            "Session command binary not found on PATH: %r (PATH=%s)",
            base_cmd,
            os.environ.get("PATH", ""),
        )
        return False, (
            f"Command not found: {base_cmd}. "
            "Ensure it is installed and in the server's PATH."
        )

    command = template.replace("{name}", shlex.quote(name))
    _log.info("Creating session '%s' with command: %s", name, command)
    proc: asyncio.subprocess.Process | None = None
    try:
        if await should_escape():
            proc = await asyncio.create_subprocess_exec(
                *wrap_shell_argv(command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,  # type: ignore[arg-type]
                start_new_session=True,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,  # type: ignore[arg-type]
                start_new_session=True,
            )
        _stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=SPAWN_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            # Some commands (amplifier-workspace) create the session then
            # try to attach (which fails without a TTY). If the session
            # exists despite the non-zero exit, treat it as success.
            try:
                sessions = await enumerate_sessions_strict(env=env)
            except (RuntimeError, FileNotFoundError) as exc:
                return False, (
                    f"Session command exited {proc.returncode} but could not "
                    f"verify session {name!r}: {exc}"
                )
            if name in sessions:
                _log.info(
                    "Session command exited %d but session '%s' exists -- "
                    "treating as success (likely a TTY-attach failure)",
                    proc.returncode,
                    name,
                )
            else:
                _log.warning(
                    "Session command exited %d: %s (stderr: %s)",
                    proc.returncode,
                    command,
                    stderr_text,
                )
                return False, (
                    f"Session command failed (exit {proc.returncode}): {stderr_text}"
                    if stderr_text
                    else f"Session command failed with exit code {proc.returncode}"
                )
    except asyncio.TimeoutError:
        _log.warning(
            "Session command still running after %ss; terminating its launcher: %s",
            SPAWN_TIMEOUT_SECONDS,
            command,
        )
        # ``wait_for`` cancels the wait, not the child.  Terminate only the
        # launcher process, not its process group: tmux's server can still be
        # in that group while it finishes daemonizing, and a group kill would
        # turn a successfully-created session into a false failure.  The
        # launcher's new session and /dev/null stdin still protect the caller
        # from a hung TTY attach.
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    proc.wait(), timeout=SPAWN_CLEANUP_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                _log.warning(
                    "Timed out after %ss waiting for terminated launcher %r; "
                    "continuing to bounded session verification",
                    SPAWN_CLEANUP_TIMEOUT_SECONDS,
                    command,
                )
            finally:
                # communicate() was cancelled by the spawn timeout. Close our
                # pipe transports even if a descendant keeps its inherited
                # descriptor open; only the launcher itself was killed above.
                _close_launcher_transport(proc)
        # A timeout is not success by itself.  Preserve the established
        # long-lived-template success only when the requested session was
        # positively observed after cleanup.
        try:
            sessions = await enumerate_sessions_strict(env=env)
        except (RuntimeError, FileNotFoundError) as exc:
            return False, (
                f"Session command timed out after {SPAWN_TIMEOUT_SECONDS}s; "
                f"could not verify session {name!r}: {exc}"
            )
        if name not in sessions:
            return False, (
                f"Session command timed out after {SPAWN_TIMEOUT_SECONDS}s "
                f"without creating session {name!r}"
            )
    except Exception as exc:
        _log.warning("Failed to launch session command %r: %s", command, exc)
        return False, f"Failed to launch command: {exc}"

    return True, None
