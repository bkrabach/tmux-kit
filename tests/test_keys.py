"""Unit tests for tmux_kit.keys -- typed-input argv builders and the
allowlist-fence mechanism (casefold + fnmatchcase).

Carried from muxplex/tests/test_input.py's "terminal_input helpers (pure
functions)" and "session_matches_allowlist" sections (plan §3.2 -- "the
casefold+fnmatchcase platform tests" are named explicitly as an incident
test that must not be stranded: fnmatch.fnmatch's case-folding is a side
effect of os.path.normcase, which is platform-dependent -- a no-op on
Linux, case-folding on macOS/Windows -- so this fence deliberately uses
explicit .casefold() + fnmatch.fnmatchcase instead). The rest of
test_input.py (the /api/sessions/{name}/input endpoint's fences,
LOCAL_ONLY_KEYS enforcement, audit logging) is muxplex application/HTTP
logic and stays in the muxplex repo -- muxplex.terminal_input is a
re-export shim over this exact module.
"""

from __future__ import annotations

import pytest
from tmux_kit.keys import (
    ALLOWED_KEYS,
    build_send_key_argv,
    build_send_text_argv,
    redact_preview,
    session_matches_allowlist,
    session_target,
)

# ---------------------------------------------------------------------------
# argv builders / constants (pure functions)
# ---------------------------------------------------------------------------


def test_session_target_is_plain_name():
    assert session_target("alpha") == "alpha"


def test_build_send_text_argv_shape():
    argv = build_send_text_argv("s1", "-rf --danger")
    assert argv == ["send-keys", "-l", "-t", "s1", "--", "-rf --danger"]


def test_build_send_key_argv_rejects_non_allowlisted():
    with pytest.raises(ValueError):
        build_send_key_argv("s1", "C-b")


def test_allowed_keys_is_the_documented_closed_set():
    assert ALLOWED_KEYS == frozenset(
        {
            "Enter",
            "Escape",
            "Tab",
            "C-c",
            "C-d",
            "Up",
            "Down",
            "Left",
            "Right",
            "PageUp",
            "PageDown",
        }
    )


def test_redact_preview_truncates_and_flattens_newlines():
    assert redact_preview("abc") == "abc"
    long = "x" * 40
    out = redact_preview(long)
    assert out.startswith("x" * 16) and out.endswith("…")
    assert "\n" not in redact_preview("a\nb\r\nc")


# ---------------------------------------------------------------------------
# session_matches_allowlist -- the pure glob-matching fence, unit-tested
# directly (no HTTP/TestClient overhead needed for these).
# ---------------------------------------------------------------------------


def test_matches_allowlist_exact_pattern_matches_only_itself():
    assert session_matches_allowlist("alpha", ["alpha"]) is True
    assert session_matches_allowlist("alphabet", ["alpha"]) is False


def test_matches_allowlist_star_matches_any_name():
    assert session_matches_allowlist("anything-goes", ["*"]) is True
    assert session_matches_allowlist("x", ["*"]) is True


def test_matches_allowlist_prefix_glob():
    assert session_matches_allowlist("amplifier-foo", ["amplifier-*"]) is True
    assert session_matches_allowlist("amplifier-test-input", ["amplifier-*"]) is True
    assert session_matches_allowlist("other-foo", ["amplifier-*"]) is False
    assert session_matches_allowlist("xamplifier-foo", ["amplifier-*"]) is False


def test_matches_allowlist_is_case_insensitive():
    """casefold() + fnmatchcase -- matching folds case deterministically
    on every platform, unlike plain fnmatch.fnmatch (whose case-folding is
    a side effect of os.path.normcase: a no-op on Linux, case-folding on
    macOS/Windows).
    """
    assert session_matches_allowlist("amplifier-foo", ["Amplifier-*"]) is True
    assert session_matches_allowlist("AMPLIFIER-Foo", ["amplifier-*"]) is True
    assert session_matches_allowlist("mysession", ["MySession"]) is True
    assert session_matches_allowlist("MYSESSION", ["MySession"]) is True
    assert session_matches_allowlist("aMpLiFiEr-test", ["AmPlIfIeR-*"]) is True


def test_matches_allowlist_empty_list_denies_everything():
    assert session_matches_allowlist("alpha", []) is False


def test_matches_allowlist_skips_non_string_entries():
    """Junk entries (int/None/dict) are skipped, not fatal; valid entries
    still match.
    """
    assert (
        session_matches_allowlist("amplifier-foo", [123, None, "amplifier-*"]) is True
    )
    assert session_matches_allowlist("alpha", [123, None, {}]) is False


def test_matches_allowlist_multiple_patterns_any_match_wins():
    assert session_matches_allowlist("amplifier-foo", ["zzz-*", "amplifier-*"]) is True
    assert session_matches_allowlist("amplifier-foo", ["amplifier-*", "zzz-*"]) is True
    assert session_matches_allowlist("neither", ["zzz-*", "amplifier-*"]) is False


def test_matches_allowlist_question_and_bracket_glob_forms():
    """`?` and `[abc]` glob forms come free with fnmatch -- document,
    don't assume.
    """
    assert session_matches_allowlist("job1", ["job?"]) is True
    assert session_matches_allowlist("job12", ["job?"]) is False
    assert session_matches_allowlist("joba", ["job[abc]"]) is True
    assert session_matches_allowlist("jobd", ["job[abc]"]) is False
