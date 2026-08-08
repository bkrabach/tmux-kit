"""Unit tests for tmux_kit.names -- session-name validation and the
'.'-mangling refusal.

Carried from muxplex/tests/test_session_rename.py (plan §3.2 -- "the
.->_ mangling refusal" is named explicitly as an incident test that must
not be stranded). The rest of that file (the rename journal, the
_migrate_session_name keyspace migration, the rename endpoint's fences,
the poll-cycle journal-completion branches) is muxplex application logic
built on top of these primitives and stays in the muxplex repo.
"""

from __future__ import annotations

from tmux_kit.names import is_tmux_stable_name, is_valid_session_name


def test_is_tmux_stable_name_rejects_dot():
    """'.' is the one character tmux mangles -- reject it."""
    assert is_tmux_stable_name("build_js") is True
    assert is_tmux_stable_name("build.js") is False
    assert is_tmux_stable_name("a.b") is False
    assert is_tmux_stable_name(".leading") is False
    assert is_tmux_stable_name("trail.") is False


def test_is_tmux_stable_name_rejects_bad_charset():
    """Requires is_valid_session_name too -- not a replacement for it."""
    assert is_tmux_stable_name("") is False
    assert is_tmux_stable_name("-leading-dash") is False
    assert is_tmux_stable_name("has:colon") is False


def test_is_valid_session_name_accepts_the_documented_charset():
    assert is_valid_session_name("build_js") is True
    assert is_valid_session_name("build.js") is True  # '.' is valid charset-wise
    assert is_valid_session_name("a" * 64) is True


def test_is_valid_session_name_rejects_leading_dash_and_colon():
    """Argument-injection and tmux target-separator guards."""
    assert is_valid_session_name("-leading-dash") is False
    assert is_valid_session_name("has:colon") is False
    assert is_valid_session_name("") is False
    assert is_valid_session_name("a" * 65) is False


def test_is_valid_session_name_rejects_trailing_newline():
    """`$` in the regex would match just before a trailing newline under
    `^...$` -- SESSION_NAME_RE uses `\\A...\\Z` specifically to close that.
    """
    assert is_valid_session_name("name\n") is False
