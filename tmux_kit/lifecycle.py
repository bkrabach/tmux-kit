"""Session lifecycle: the "stop it" half spawn.py doesn't cover.

Real gap found while building the 0.2.0 facade: ``spawn.py`` gives a
consumer everything needed to CREATE a session, but nothing to end one --
every existing caller had to drop to ``proc.run_tmux("kill-session", ...)``
by hand, re-deriving the right target syntax and error handling each time.
This module is that missing counterpart, kept as small and single-purpose
as ``spawn.py``'s own contract.

Deliberately NOT built here (see ``tmux_kit/CONSUMERS.md``'s "NOT in the
library yet"): a typed ``Sender``/``SendPolicy`` object, or any allowlist
policy layer -- ``kill_session``/``interrupt_session`` are raw, unguarded
primitives, same trust model as ``proc.run_tmux()`` itself. A caller at a
security boundary (an HTTP API, a multi-tenant agent) MUST apply its own
authorization check before calling either, exactly as it must before
calling ``spawn_session`` or the ``tmux_kit.keys`` send helpers.
"""

from __future__ import annotations

from tmux_kit.keys import build_send_key_argv
from tmux_kit.proc import run_tmux


async def kill_session(name: str) -> None:
    """Run ``tmux kill-session -t =<name>`` (argv, no shell) -- a hard,
    immediate stop.

    Uses ``=<name>`` (tmux's exact-match target form -- see
    ``names.rename_tmux_session()``'s docstring for why this beats a plain
    ``-t name`` prefix-matchable target) so this cannot land on a
    differently-named neighbour.

    Raises RuntimeError (via ``run_tmux`` -- tmux's own stderr, e.g.
    ``can't find session``) if *name* does not exist or tmux is
    unreachable. Callers that want "kill it if it exists, otherwise do
    nothing" should catch that themselves -- this function does not
    swallow it, matching ``rename_tmux_session()``'s existing convention of
    surfacing tmux's refusal rather than guessing at its meaning.
    """
    await run_tmux("kill-session", "-t", f"={name}")


async def interrupt_session(name: str) -> None:
    """Send Ctrl-C to *name*'s active pane -- a graceful "stop the running
    command" request, not a session kill.

    This is exactly ``tmux send-keys -t <name> C-c``, expressed through the
    same ``tmux_kit.keys`` argv builder (and therefore the same
    ``ALLOWED_KEYS`` closed set) every other typed-input path in this
    package uses -- there is no second, parallel "how do I send C-c"
    mechanism. The session and its pane are left running afterward
    (whatever the foreground process does with SIGINT is up to it -- most
    interactive REPLs/shells survive it; a job that doesn't trap it exits,
    at which point ``observe.pane_is_dead()`` reports the pane as dead).

    Raises RuntimeError (via ``run_tmux``) if *name* does not exist or tmux
    is unreachable, same as ``kill_session``.
    """
    await run_tmux(*build_send_key_argv(name, "C-c"))
