"""tmux session-name validation (security boundary) and exact-match rename.

Moved verbatim from ``sessions.py`` (tmux-lib extraction stage S1, plan
§7.1). These are tmux's own facts -- its target-separator charset, its
silent ``.``->``_`` mangling, its exact-match ``=target`` form -- not
muxplex facts, which is why they are library (plan §2).
"""

from __future__ import annotations

import re

from tmux_kit.proc import run_tmux

# ---------------------------------------------------------------------------
# Session-name validation (security boundary)
# ---------------------------------------------------------------------------

# Canonical allowlist for client-supplied session names. A name that matches
# this pattern contains no shell metacharacters, whitespace, or the tmux target
# separator (`:`), so it is safe to substitute into a shell template
# (create/delete session commands) and safe as a `tmux -t` target.
#
# This is the PRIMARY defense against shell injection via session names. Every
# API endpoint that accepts a client-supplied session name and forwards it to a
# subprocess (create, delete, connect, and any future input endpoint) MUST run
# the name through `is_valid_session_name()` at the boundary, BEFORE any
# substitution or subprocess call.
#
# Charset rationale: tmux forbids `:` in session names (it's the
# session:window.pane target separator), so excluding it costs nothing. All 68
# of the deployment's live session names pass this pattern; it does not reject
# any legitimate existing name.
# The first character MUST be alphanumeric or underscore. This is deliberate and
# security-load-bearing: a leading ``-`` would let a valid name be parsed as an
# OPTION by tmux or by a user-configurable template command (argument injection),
# and ``shlex.quote`` does NOT neutralize that -- quoting stops shell-metacharacter
# interpretation, but a quoted ``-C`` or ``--destroy`` is still a flag to the
# invoked program. Forbidding a leading ``-`` (and leading ``.``/``..`` path
# traversal) closes that class. ``\A...\Z`` (not ``^...$``) is required because
# ``$`` also matches just before a trailing newline, so ``"name\n"`` would slip
# through ``^...$``. All 68 live session names pass this pattern.
# The maximum session-name LENGTH, in characters. Raised from 64 to 255 in
# 0.5.0; the 64 was arbitrary and rejected names that both tmux and the
# filesystem would have accepted without complaint.
#
# 255 is NOT a tmux limit -- tmux has none. Measured against a real tmux 3.4
# on an isolated (``-L``) server: names of 64, 65, 100, 128, 200, 254, 255,
# 256, 300, 512, 1024 and 4096 characters ALL created with rc=0 and
# round-tripped at full length through ``list-sessions``. No length was found
# at which tmux refuses or truncates.
#
# 255 is the FILESYSTEM's limit -- POSIX ``NAME_MAX``, and the value on ext4,
# xfs, btrfs, APFS and every mainstream Linux/macOS filesystem: a single path
# COMPONENT may be at most 255 BYTES. It binds here because consumers name a
# DIRECTORY after the session (muxplex's configured ``new_session_template``
# is ``amplifier-workspace ~/dev/{name}``, making the session name a
# directory basename verbatim). Measured on ext4: ``mkdir`` of a
# 255-character name succeeds; 256 fails ENAMETOOLONG ("File name too long").
# ``getconf NAME_MAX`` reports 255.
#
# CHARACTERS vs BYTES -- load-bearing, do not skip. ``NAME_MAX`` is a BYTE
# budget, but this regex's charset is ASCII-only (``[A-Za-z0-9_.-]``), so
# every accepted character is exactly ONE UTF-8 byte and a 255-CHARACTER cap
# IS a 255-byte cap. That equivalence is the only reason this constant can be
# a character count at all. If the charset is ever widened to non-ASCII, this
# must become an encoded-byte-length check, NOT a ``len()`` check -- otherwise
# a 255-character name of multi-byte characters passes validation here and
# then fails at ``mkdir`` in the consumer.
#
# A consumer that embeds the name in a LONGER basename (a ``<name>.sock``
# sibling, a ``<prefix>-<name>`` directory) has less than 255 bytes of room
# and MUST enforce its own, stricter cap -- this library cannot know that
# consumer's prefix, and guessing one here would be policy, not mechanism
# (AGENTS.md's scope litmus test). tmux-kit itself never derives a filename
# from a session name (its socket dir is fixed and name-independent), so 255
# is the right bound HERE: the largest value that is wrong for no consumer,
# rather than a smaller guess that is wrong for all of them.
SESSION_NAME_MAX_LEN: int = 255

# Built FROM the constant so the two can never drift. The quantifier is
# ``SESSION_NAME_MAX_LEN - 1`` because the leading-character class already
# consumes one character -- an off-by-one here would silently ship a 254- or
# 256-char cap that disagrees with the constant every error message quotes
# (pinned by ``tests/test_names.py``'s constant/regex agreement test).
SESSION_NAME_RE = re.compile(
    r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0," + str(SESSION_NAME_MAX_LEN - 1) + r"}\Z"
)


def is_valid_session_name(name: str) -> bool:
    """Return True if *name* is a safe session name per ``SESSION_NAME_RE``.

    Safe means: 1 to ``SESSION_NAME_MAX_LEN`` (255) chars drawn only from
    ASCII letters, digits, and the ``_ . -`` set, with an
    alphanumeric-or-underscore FIRST character -- no whitespace (including a
    trailing newline), no shell metacharacters, no ``:``, and no leading
    ``-`` (argument injection) or leading ``.``/``..`` (path traversal).
    Callers at the API boundary reject names that fail this check with HTTP
    400 before the name reaches any subprocess.

    The length bound is the FILESYSTEM's (``NAME_MAX``, 255 bytes), not
    tmux's -- tmux has no session-name length limit at all. A caller
    rendering a rejection message should quote ``SESSION_NAME_MAX_LEN``
    rather than hardcoding a number: this cap was 64 before 0.5.0, and a
    hardcoded message is exactly what went stale when it moved. A consumer
    that needs a STRICTER cap (because it embeds the name in a longer
    basename) enforces that itself -- see ``SESSION_NAME_MAX_LEN``'s comment.
    """
    return bool(SESSION_NAME_RE.match(name))


def is_tmux_stable_name(name: str) -> bool:
    """Return True if tmux would create/rename a session to EXACTLY *name*,
    with no silent character mangling.

    ``SESSION_NAME_RE`` (``is_valid_session_name``) permits ``.`` in a
    session name, but tmux 3.4 silently converts ``.`` to ``_`` at
    creation/rename time -- verified empirically against a real, isolated
    tmux server (see docs/plans/2026-08-07-session-rename-plan.md \\u00a71): a
    session named via ``build.js`` actually comes out as ``build_js``, with
    exit code 0 and no error. That gap is the entire mangling problem: a
    caller requesting ``build.js`` cannot tell from the response alone that
    it got ``build_js`` instead.

    This predicate lets a caller REJECT such a request outright rather than
    predict tmux's mangling rule. Rejecting is deliberately preferred over
    modeling the substitution (``requested.replace(".", "_")``): a wrong
    prediction produces a wrong collision check and a silently mis-keyed
    session, whereas over-rejecting on a hypothetical tmux that would not
    mangle the name costs the caller one retry.

    Requires *name* to ALSO pass ``is_valid_session_name`` first -- this is
    an ADDITIONAL, stricter check for names that must survive tmux
    unchanged (currently: ``POST /api/sessions/{name}/rename``'s
    ``new_name``), not a replacement for the charset boundary every
    session-name-accepting endpoint already enforces via
    ``is_valid_session_name``. Deliberately NOT applied to the create path
    (``POST /api/sessions``) -- that is a separate, pre-existing, and
    breaking fix left for the owner to decide on its own (see the rename
    plan \\u00a73/\\u00a715).
    """
    return is_valid_session_name(name) and "." not in name


async def rename_tmux_session(old_name: str, new_name: str) -> None:
    """Run ``tmux rename-session -t =<old_name> -- <new_name>`` (argv, no
    shell).

    Raises RuntimeError (via ``run_tmux`` -- tmux's own stderr, e.g.
    ``duplicate session: <new_name>``) if tmux refuses, notably rc=1 when
    *new_name* is already a live session.

    Uses ``=<old_name>`` -- tmux's EXACT-match target form, verified live
    to work for ``rename-session`` (unlike a ``send-keys`` pane target;
    see ``terminal_input.session_target``'s docstring) -- plus ``--``
    end-of-options, giving this call a STRONGER targeting guarantee than
    ``/input`` achieves: tmux resolves an exact-match target before any
    prefix matching, so this cannot land on a differently-named neighbour.
    This is the first session-lifecycle subprocess with no shell path at
    all -- callers still validate both names first
    (``is_valid_session_name`` / ``is_tmux_stable_name``), same discipline
    as every other tmux-touching endpoint.

    tmux reports rc=0 even when it silently mangles the resulting name
    (see ``is_tmux_stable_name``'s docstring) -- callers MUST re-enumerate
    and verify the observed name after this call succeeds; this function
    only reports whether tmux accepted the request, never what the
    session ended up named.
    """
    await run_tmux("rename-session", "-t", f"={old_name}", "--", new_name)
