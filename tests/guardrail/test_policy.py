"""guardrail.policy: risk classification, allowlist, evaluate, redaction."""

import pytest

from guardrail.policy import (
    check_allowed,
    classify_risk,
    evaluate,
    redact_type_value,
    resolve_max_auto_tier,
)
from schema.action import ClickAction, Target, TypeAction
from schema.guardrail import RiskTier

APP = "http://127.0.0.1:5001"


def _click(value):
    return ClickAction(action="click", target=Target(strategy="text_contains", value=value))


def _type(value="Member number", text="10003"):
    return TypeAction(action="type", target=Target(strategy="label", value=value), text=text)


# --- classify_risk ------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("Retrieve", RiskTier.SAFE),
    ("Cancel", RiskTier.SAFE),
    ("Process", RiskTier.CONFIRM),
    ("Open sub-account", RiskTier.CONFIRM),
    ("Submit request", RiskTier.CONFIRM),
    ("Create account", RiskTier.CONFIRM),
    ("Delete member", RiskTier.BLOCKED),
    ("Remove hold", RiskTier.BLOCKED),
])
def test_classify_risk_of_clicks(value, expected):
    assert classify_risk(_click(value)) is expected


def test_classify_risk_is_case_insensitive():
    assert classify_risk(_click("PROCESS")) is RiskTier.CONFIRM
    assert classify_risk(_click("role:button DELETE")) is RiskTier.BLOCKED


def test_type_into_a_non_sensitive_field_is_safe():
    assert classify_risk(_type(value="Member number", text="10003")) is RiskTier.SAFE
    assert classify_risk(_type(value="Initial deposit", text="500")) is RiskTier.SAFE


@pytest.mark.parametrize("label", [
    "Passphrase", "Password", "Verify member SSN", "Tax ID", "Enter PIN",
    "role:textbox Verify member SSN", "PASSPHRASE",
])
def test_type_into_a_sensitive_field_is_unconditionally_blocked(label):
    # blocked regardless of AGENT_MAX_AUTO_RISK_TIER -- same category as a
    # destructive click; the agent must never autofill a secret.
    action = _type(value=label, text="whatever the agent guessed")
    assert classify_risk(action) is RiskTier.BLOCKED
    d = evaluate(action, f"{APP}/x", APP, RiskTier.BLOCKED)   # max tier = blocked
    assert d.allowed_automatically is False
    assert d.tier is RiskTier.BLOCKED


# --- check_allowed ----------------------------------------------------


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:5001/member/10003", True),
    ("http://127.0.0.1:5001", True),
    ("http://127.0.0.1:5001/member/10003/sub-account", True),
    ("http://evil.example.com/member", False),
    ("https://127.0.0.1:5001/member", False),     # scheme mismatch
    ("http://127.0.0.1:9999/member", False),       # port mismatch
    ("", False),
])
def test_check_allowed(url, ok):
    assert check_allowed(url, APP) is ok


def test_check_allowed_respects_a_base_path():
    base = "http://127.0.0.1:5001/member"
    assert check_allowed("http://127.0.0.1:5001/member/10003", base) is True
    assert check_allowed("http://127.0.0.1:5001/login", base) is False


# --- evaluate --------------------------------------------------------


def test_safe_action_within_max_tier_is_auto():
    d = evaluate(_type(), f"{APP}/member", APP, RiskTier.SAFE)
    assert d.allowed_automatically is True
    assert d.tier is RiskTier.SAFE
    assert d.allowlist_violation is False


def test_confirm_action_above_max_tier_is_not_auto():
    d = evaluate(_click("Process"), f"{APP}/member/10003/sub-account", APP, RiskTier.SAFE)
    assert d.allowed_automatically is False
    assert d.tier is RiskTier.CONFIRM


def test_confirm_action_at_max_tier_is_auto():
    d = evaluate(_click("Process"), f"{APP}/x", APP, RiskTier.CONFIRM)
    assert d.allowed_automatically is True
    assert d.tier is RiskTier.CONFIRM


def test_blocked_action_never_auto_even_at_max_blocked():
    d = evaluate(_click("Delete member"), f"{APP}/x", APP, RiskTier.BLOCKED)
    assert d.allowed_automatically is False
    assert d.tier is RiskTier.BLOCKED


def test_allowlist_violation_always_blocks_regardless_of_tier():
    # a plainly safe action, but off-app -> still not auto, and flagged
    d = evaluate(_type(), "http://evil.example.com/", APP, RiskTier.BLOCKED)
    assert d.allowed_automatically is False
    assert d.allowlist_violation is True
    assert "allowlist violation" in d.reason


# --- resolve_max_auto_tier + redaction ------------------------------


def test_resolve_max_auto_tier_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_AUTO_RISK_TIER", "confirm")
    assert resolve_max_auto_tier() is RiskTier.CONFIRM
    monkeypatch.delenv("AGENT_MAX_AUTO_RISK_TIER", raising=False)
    assert resolve_max_auto_tier() is RiskTier.SAFE
    monkeypatch.setenv("AGENT_MAX_AUTO_RISK_TIER", "garbage")
    assert resolve_max_auto_tier() is RiskTier.SAFE


@pytest.mark.parametrize("label,value,expected", [
    ("Passphrase", "vault", "[REDACTED]"),
    ("Verify member SSN", "912-18-2247", "[REDACTED]"),
    ("Enter PIN", "1234", "[REDACTED]"),
    ("Tax ID", "99-0000000", "[REDACTED]"),
    ("Member number", "10003", "10003"),
    ("Initial deposit", "500.00", "500.00"),
])
def test_redact_type_value(label, value, expected):
    assert redact_type_value(label, value) == expected
