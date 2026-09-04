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

import os

import pytest

from tmux_kit.names import (
    SESSION_NAME_MAX_LEN,
    SESSION_NAME_RE,
    is_tmux_stable_name,
    is_valid_session_name,
)


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


# ---------------------------------------------------------------------------
# The length cap (raised 64 -> 255 in 0.5.0)
#
# The 64 was arbitrary: measured against a real tmux 3.4 on an isolated
# (`-L`) server, session names of 64, 65, 100, 128, 200, 254, 255, 256, 300,
# 512, 1024 and 4096 characters ALL created with rc=0 and round-tripped at
# full length. The real bound is the FILESYSTEM's NAME_MAX (255 bytes),
# because a consumer names a directory after the session.
# ---------------------------------------------------------------------------


def test_the_cap_is_the_filesystem_name_max_not_the_old_arbitrary_64():
    """A regression guard on the NUMBER itself.

    255 is not a taste call -- it is POSIX `NAME_MAX`. If someone lowers this
    back toward 64 without moving the filesystem, this test says so.
    """
    assert SESSION_NAME_MAX_LEN == 255


def test_a_100_char_name_is_accepted():
    """The specific case a user was refused for, at 65+ characters, for a
    name tmux and the filesystem would both have accepted.
    """
    assert is_valid_session_name("a" * 100) is True


def test_length_boundary_is_exactly_session_name_max_len():
    """Off-by-one fence. The regex's quantifier is `MAX_LEN - 1` because the
    leading-character class already consumes one character -- get that wrong
    and the shipped cap silently disagrees with the constant every error
    message quotes.
    """
    assert is_valid_session_name("a" * (SESSION_NAME_MAX_LEN - 1)) is True
    assert is_valid_session_name("a" * SESSION_NAME_MAX_LEN) is True
    assert is_valid_session_name("a" * (SESSION_NAME_MAX_LEN + 1)) is False
    assert is_valid_session_name("a" * (SESSION_NAME_MAX_LEN * 2)) is False


def test_regex_and_constant_cannot_drift():
    """SESSION_NAME_RE is BUILT from SESSION_NAME_MAX_LEN. This asserts the
    compiled pattern actually reflects the constant, so a future hand-edit of
    the regex literal that forgets the constant (or vice versa) fails here
    rather than in a consumer's error message.
    """
    at_cap = "a" * SESSION_NAME_MAX_LEN
    over_cap = "a" * (SESSION_NAME_MAX_LEN + 1)
    assert SESSION_NAME_RE.match(at_cap) is not None
    assert SESSION_NAME_RE.match(over_cap) is None


def test_char_cap_equals_byte_cap_because_the_charset_is_ascii_only():
    """NAME_MAX is a BYTE budget; this cap is a CHARACTER count. Those are
    the same number only because the charset is ASCII-only -- the property
    the constant's comment calls load-bearing. If the charset is ever widened
    to non-ASCII, this test fails and the cap must become an encoded-byte
    check, not a len() check.
    """
    longest_valid = "a" * SESSION_NAME_MAX_LEN
    assert is_valid_session_name(longest_valid) is True
    assert len(longest_valid.encode("utf-8")) == SESSION_NAME_MAX_LEN

    # Every character the regex admits is single-byte.
    for ch in "abzABZ019_.-":
        assert len(ch.encode("utf-8")) == 1
    # And a multi-byte character is rejected outright, so no accepted name
    # can ever exceed its character count in bytes.
    assert is_valid_session_name("caf\u00e9") is False
    assert is_valid_session_name("na\u00efve-session") is False


def test_the_longest_valid_name_actually_fits_on_the_filesystem(tmp_path):
    """The empirical half of the derivation: the cap is only correct if a
    name at exactly SESSION_NAME_MAX_LEN can really be a directory basename,
    and one character more cannot.

    Skipped where the test filesystem's NAME_MAX differs (it is 255 on ext4,
    xfs, btrfs and APFS) -- the point is to prove the number against a real
    filesystem when we are on one, not to assert every filesystem agrees.
    """
    name_max = os.pathconf(str(tmp_path), "PC_NAME_MAX")
    if name_max != SESSION_NAME_MAX_LEN:
        pytest.skip(
            f"test filesystem NAME_MAX is {name_max}, not "
            f"{SESSION_NAME_MAX_LEN} -- cannot prove the bound here"
        )

    longest_valid = "a" * SESSION_NAME_MAX_LEN
    assert is_valid_session_name(longest_valid) is True
    (tmp_path / longest_valid).mkdir()  # must not raise ENAMETOOLONG

    one_too_long = "a" * (SESSION_NAME_MAX_LEN + 1)
    assert is_valid_session_name(one_too_long) is False
    with pytest.raises(OSError):  # ENAMETOOLONG -- the reason the cap exists
        (tmp_path / one_too_long).mkdir()


def test_raising_the_cap_did_not_relax_any_other_rule_at_long_lengths():
    """The security controls are the LEADING-CHARACTER rule and the charset,
    not the length -- and only the length quantifier moved. A long name must
    still be rejected for the same reasons a short one is.

    This is the test that would catch a "fix" that widened the cap by
    loosening the anchor or the first-character class along with it.
    """
    pad = "a" * 200

    # Leading '-' : argument injection (a quoted '-C' is still a FLAG).
    assert is_valid_session_name("-" + pad) is False
    assert is_valid_session_name("--destroy-" + pad) is False
    # Leading '.' / '..' : path traversal in the name-derived directory.
    assert is_valid_session_name("." + pad) is False
    assert is_valid_session_name(".." + pad) is False
    # tmux's target separator.
    assert is_valid_session_name(pad + ":" + pad[:20]) is False
    # Shell metacharacters and whitespace.
    assert is_valid_session_name(pad + " " + pad[:20]) is False
    assert is_valid_session_name(pad + ";rm -rf /") is False
    assert is_valid_session_name(pad + "$(id)") is False
    # \A...\Z, not ^...$ -- a trailing newline must not slip through at any
    # length.
    assert is_valid_session_name(pad + "\n") is False


def test_is_tmux_stable_name_still_refuses_dot_at_the_new_length():
    """The '.'-mangling refusal is independent of the length cap."""
    assert is_tmux_stable_name("a" * 200) is True
    assert is_tmux_stable_name("a" * 100 + "." + "b" * 99) is False
    assert is_tmux_stable_name("a" * (SESSION_NAME_MAX_LEN + 1)) is False


def test_is_valid_session_name_rejects_trailing_newline():
    """`$` in the regex would match just before a trailing newline under
    `^...$` -- SESSION_NAME_RE uses `\\A...\\Z` specifically to close that.
    """
    assert is_valid_session_name("name\n") is False
