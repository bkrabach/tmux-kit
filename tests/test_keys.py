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
    build_exit_copy_mode_argv,
    build_send_key_argv,
    build_send_text_argv,
    destructive_action_allowed,
    input_allowed_for_session,
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
    """0.4.0: the argv is now TWO tmux commands chained via a literal ``;``
    -- ``copy-mode -q -t <target>`` ahead of the ``send-keys`` call -- not
    the bare ``send-keys`` call alone. See keys.py's docstring and
    CHANGELOG's 0.4.0 entry: this used to be a separate builder
    (``build_exit_copy_mode_argv``) callers had to remember to invoke
    first, which is exactly the shape that let
    ``lifecycle.interrupt_session()`` (and therefore ``api.stop()``)
    silently fail to interrupt a pane stuck in copy-mode.
    """
    argv = build_send_text_argv("s1", "-rf --danger")
    assert argv == [
        "copy-mode",
        "-q",
        "-t",
        "s1",
        ";",
        "send-keys",
        "-l",
        "-t",
        "s1",
        "--",
        "-rf --danger",
    ]


def test_build_send_text_argv_composes_build_exit_copy_mode_argv():
    """The chained prefix must be EXACTLY ``build_exit_copy_mode_argv()``'s
    own return value -- one literal source, not a hand-duplicated copy
    that could silently drift from it.
    """
    name = "s1"
    prefix = build_exit_copy_mode_argv(name)
    argv = build_send_text_argv(name, "hello")
    assert argv[: len(prefix)] == prefix
    assert argv[len(prefix)] == ";"


def test_build_send_text_argv_keeps_literal_semicolon_as_one_argv_element():
    """A literal ``;`` INSIDE the text must stay inside the single,
    ``--``-terminated ``send-keys`` argument -- never split out into a
    second tmux command. This is the property that makes chaining safe:
    ``;`` is only a tmux command separator when it is its OWN argv
    element, never as a substring of a later element. Verified against
    real tmux 3.4 with this exact hostile shape (see CHANGELOG 0.4.0).
    """
    hostile = "; rm -rf / && $(reboot) `id` | tee /etc/passwd"
    argv = build_send_text_argv("s1", hostile)
    # Exactly one command separator: the one WE inserted between
    # copy-mode and send-keys. The hostile text does NOT contribute a
    # second one, because it travels as a single argv element.
    assert argv.count(";") == 1
    assert argv[-1] == hostile


def test_build_send_key_argv_shape():
    """0.4.0: same chaining as build_send_text_argv() -- see that test's
    docstring.
    """
    argv = build_send_key_argv("s1", "C-c")
    assert argv == [
        "copy-mode",
        "-q",
        "-t",
        "s1",
        ";",
        "send-keys",
        "-t",
        "s1",
        "C-c",
    ]


def test_build_send_key_argv_composes_build_exit_copy_mode_argv():
    name = "s1"
    prefix = build_exit_copy_mode_argv(name)
    argv = build_send_key_argv(name, "C-c")
    assert argv[: len(prefix)] == prefix
    assert argv[len(prefix)] == ";"


def test_build_send_key_argv_rejects_non_allowlisted():
    with pytest.raises(ValueError):
        build_send_key_argv("s1", "C-b")


def test_build_exit_copy_mode_argv_shape():
    argv = build_exit_copy_mode_argv("s1")
    assert argv == ["copy-mode", "-q", "-t", "s1"]


def test_build_exit_copy_mode_argv_does_not_use_the_equals_name_form():
    """``copy-mode`` takes a pane target, same as ``send-keys`` -- the
    ``=name`` exact-match prefix is not valid here either (see
    ``session_target()``'s docstring), so the target must be the plain
    name, never ``=s1``.
    """
    argv = build_exit_copy_mode_argv("s1")
    assert "=s1" not in argv
    assert argv[argv.index("-t") + 1] == "s1"


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


# ---------------------------------------------------------------------------
# destructive_action_allowed -- the deny-by-default fence gating the MCP
# server's `stop`/`kill` tools (see tmux_kit/mcp_server.py).
# ---------------------------------------------------------------------------


def test_destructive_action_denied_when_not_enabled():
    """Absent/false ``enabled`` denies every name, even a matching allow
    pattern -- fail closed, never fail open."""
    assert destructive_action_allowed("alpha", {"allow": ["*"]}) is False
    assert (
        destructive_action_allowed("alpha", {"enabled": False, "allow": ["*"]}) is False
    )


def test_destructive_action_denied_by_truthy_non_bool_enabled():
    """Only the literal boolean True counts -- a truthy string (e.g. from
    a hand-edited config) must not enable the fence."""
    assert (
        destructive_action_allowed("alpha", {"enabled": "true", "allow": ["*"]})
        is False
    )
    assert destructive_action_allowed("alpha", {"enabled": 1, "allow": ["*"]}) is False


def test_destructive_action_denied_when_allow_not_a_list():
    assert (
        destructive_action_allowed("alpha", {"enabled": True, "allow": "alpha"})
        is False
    )
    assert destructive_action_allowed("alpha", {"enabled": True}) is False


def test_destructive_action_allowed_matches_glob_when_enabled():
    policy = {"enabled": True, "allow": ["demo-*"]}
    assert destructive_action_allowed("demo-1", policy) is True
    assert destructive_action_allowed("other", policy) is False


def test_destructive_action_allowed_is_case_insensitive_like_the_underlying_fence():
    policy = {"enabled": True, "allow": ["Demo-*"]}
    assert destructive_action_allowed("demo-1", policy) is True


# ---------------------------------------------------------------------------
# destructive_action_allowed / input_allowed_for_session -- the full
# malformed-input matrix. A fence whose unconfigured or malformed path
# *raises* is not fail-closed in practice: whether the caller ends up
# denying or granting the action then depends on that caller's exception
# handling, which is exactly the ambiguity these functions exist to remove.
# ``policy=None`` ("no policy configured") is the single most likely input
# in a real deployment (operator hasn't set any env vars / config file is
# absent) -- every case below must return False, never raise.
# ---------------------------------------------------------------------------

# (description, policy_or_settings) -- shared across both fence functions
# since destructive_action_allowed generalizes input_allowed_for_session's
# contract and both must reject the same malformed shapes.
_MALFORMED_POLICIES = [
    ("none", None),
    ("empty dict", {}),
    ("wrong type: bare string", "prod-db"),
    ("wrong type: bare list", ["*"]),
    ("wrong type: bare int", 1),
    ("enabled missing, allow present", {"allow": ["*"]}),
    ("enabled string 'true' (not literal True)", {"enabled": "true", "allow": ["*"]}),
    ("enabled int 1 (not literal True)", {"enabled": 1, "allow": ["*"]}),
    ("enabled True, allow missing", {"enabled": True}),
    ("enabled True, allow is a string", {"enabled": True, "allow": "demo-*"}),
    ("enabled True, allow is an int", {"enabled": True, "allow": 1}),
    ("enabled True, allow is None", {"enabled": True, "allow": None}),
    (
        "enabled True, allow has non-string entries only",
        {"enabled": True, "allow": [1, None, {}]},
    ),
]


@pytest.mark.parametrize("description,policy", _MALFORMED_POLICIES)
def test_destructive_action_allowed_never_raises_and_always_denies(description, policy):
    """Every malformed/absent/hostile policy shape must deny, never raise.

    ``policy=None`` in particular is the realistic default in production
    (see AGENTS.md's MCP deny-by-default section) -- a caller with no
    policy configured must get a clean ``False``, not an AttributeError.
    """
    assert destructive_action_allowed("prod-db", policy) is False, description


@pytest.mark.parametrize("description,policy", _MALFORMED_POLICIES)
def test_input_allowed_for_session_never_raises_and_always_denies(description, policy):
    """``input_allowed_for_session`` shares its fail-closed contract with
    ``destructive_action_allowed`` (the latter generalizes the former) --
    the same malformed-shape matrix must deny, never raise, here too.
    """
    assert input_allowed_for_session("prod-db", policy) is False, description


def test_destructive_action_allowed_still_authorizes_a_well_formed_policy():
    """The fix must not turn the fence into permanent deny -- a genuinely
    well-formed, enabled policy with a matching pattern still authorizes."""
    assert (
        destructive_action_allowed("demo-1", {"enabled": True, "allow": ["demo-*"]})
        is True
    )


def test_input_allowed_for_session_still_authorizes_a_well_formed_policy():
    assert (
        input_allowed_for_session(
            "demo-1",
            {"input_enabled": True, "input_allowed_sessions": ["demo-*"]},
        )
        is True
    )
