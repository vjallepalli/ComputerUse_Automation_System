"""Adversarial edge cases for guardrail.policy -- Unicode keyword evasion,
substring false-positives, degenerate targets, and allowlist boundary tricks.

Companion to test_policy.py (the happy-path table). Written during the
self-directed adversarial pass; the Unicode-fold cases are regressions for a
genuine bug (fullwidth / zero-width text slipping past the keyword checks).
"""

import pytest
from pydantic import ValidationError

from guardrail.policy import (
    check_allowed,
    classify_risk,
    normalize_for_keywords,
    redact_type_value,
)
from schema.action import ClickAction, Target, TypeAction
from schema.guardrail import RiskTier

APP = "http://127.0.0.1:5001"


def _click(value, strategy="text_contains"):
    return ClickAction(action="click", target=Target(strategy=strategy, value=value))


def _type(value, text="whatever the agent guessed"):
    return TypeAction(action="type", target=Target(strategy="label", value=value), text=text)


# --- BUG: Unicode look-alike / zero-width keyword evasion -------------------
# Before the fix, `_classify` folded the target with a bare `.lower()`, so a
# control whose accessible name was spelled with fullwidth glyphs or had a
# zero-width space wedged in did NOT match WRITE/DESTRUCTIVE/SENSITIVE keywords
# and was auto-executed as `safe`. `normalize_for_keywords` (NFKC + strip Cf +
# casefold) closes that.


@pytest.mark.parametrize("value", [
    "ＳＵＢＭＩＴ",           # fullwidth "SUBMIT"
    "Ｐｒｏｃｅｓｓ",      # fullwidth "process"
    "Sub​mit",                                    # zero-width space
    "pro‌cess request",                           # zero-width non-joiner
    "confirm﻿",                                    # BOM / zero-width no-break space
    "sub­mit",                                    # soft hyphen
])
def test_obfuscated_write_keyword_still_classifies_as_confirm(value):
    assert classify_risk(_click(value)) is RiskTier.CONFIRM


@pytest.mark.parametrize("value", [
    "Ｄｅｌｅｔｅ",            # fullwidth "Delete"
    "Re​move hold",                               # zero-width space in "Remove"
])
def test_obfuscated_destructive_keyword_still_blocks(value):
    assert classify_risk(_click(value)) is RiskTier.BLOCKED


@pytest.mark.parametrize("label", [
    "Verify member ＳＳＮ",                # fullwidth "SSN"
    "S​SN",                                        # zero-width space
    "Enter ＰＩＮ",                        # fullwidth "PIN"
    "pass­word",                                   # soft hyphen in "password"
])
def test_obfuscated_sensitive_label_still_blocks_a_type(label):
    # a `type` into a sensitive field is unconditionally blocked -- the fold
    # must not let an obfuscated label leak the field past the guardrail.
    assert classify_risk(_type(label)) is RiskTier.BLOCKED


def test_redactor_folds_obfuscated_sensitive_label():
    assert redact_type_value("S​SN", "123-45-6789") == "[REDACTED]"
    assert redact_type_value("Ｐａｓｓｗｏｒｄ", "hunter2") == "[REDACTED]"
    # a genuinely non-sensitive label is still passed through untouched
    assert redact_type_value("Member number", "10003") == "10003"


def test_normalize_for_keywords_is_idempotent_on_plain_ascii():
    assert normalize_for_keywords("Open sub-account") == "open sub-account"
    assert normalize_for_keywords("") == ""
    assert normalize_for_keywords(None) == ""


# --- substring false-positives (documenting the heuristic's known edges) ----


@pytest.mark.parametrize("value", [
    "Assignment history",     # contains "sign" -- but "sign" is not a keyword
    "Design review",          # contains "sign"
    "Preview details",        # contains nothing
    "Members area",           # contains "member" -- not a keyword either
])
def test_benign_words_that_merely_look_keyword_ish_stay_safe(value):
    assert classify_risk(_click(value)) is RiskTier.SAFE


def test_keyword_as_substring_of_a_benign_label_over_blocks_by_design():
    # "Removed items (view)" is a read-only link, but "remove" is a substring ->
    # BLOCKED. This is a deliberate false-positive: the heuristic errs toward
    # escalation, never toward silently auto-running a write. Documented, not a
    # bug -- see guardrail/policy.py module docstring.
    assert classify_risk(_click("Removed items (view)")) is RiskTier.BLOCKED
    assert classify_risk(_click("Reprocess nightly batch")) is RiskTier.CONFIRM


# --- degenerate targets ---------------------------------------------------


def test_empty_target_value_is_rejected_by_the_schema():
    with pytest.raises(ValidationError):
        Target(strategy="text_contains", value="")


def test_whitespace_only_target_classifies_safe_and_does_not_raise():
    # JUDGMENT CALL: `Target.value` only enforces min_length=1, so a single
    # space slips through. It matches no keyword -> `safe`. That is acceptable:
    # a whitespace locator resolves to nothing downstream and replay turns that
    # into a clean SelectorResolutionError / FailureDetail, not a crash. Not
    # worth a dedicated schema rule.
    assert classify_risk(_click("   ")) is RiskTier.SAFE
    assert classify_risk(_type("   ", text="x")) is RiskTier.SAFE


def test_mixed_case_write_keyword():
    assert classify_risk(_click("SuBmIt ReQuEsT")) is RiskTier.CONFIRM


# --- allowlist boundary tricks ------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:5001.evil.com/member/10003",     # suffix host, not a match
    "http://evil.com/?x=http://127.0.0.1:5001",         # base URL only in a query param
    "https://127.0.0.1:5001/member/10003",              # scheme differs
    "http://127.0.0.1:5002/member/10003",               # port differs
    "http://127.0.0.1:5001@evil.com/",                   # userinfo trick
])
def test_lookalike_hosts_are_not_inside_the_allowlist(url):
    assert check_allowed(url, APP) is False


def test_path_prefix_is_a_segment_boundary_not_a_string_prefix():
    base = "http://h.example/app"
    assert check_allowed("http://h.example/app", base) is True
    assert check_allowed("http://h.example/app/x", base) is True
    assert check_allowed("http://h.example/application", base) is False   # not /app or /app/*


def test_empty_url_or_base_is_a_hard_block():
    assert check_allowed("", APP) is False
    assert check_allowed(APP, "") is False
