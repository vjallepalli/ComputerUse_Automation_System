"""Thin-but-real guardrail: a keyword risk heuristic + a target-app allowlist +
a keyword secret-redactor. Imported by BOTH /agent and /replay (CLAUDE.md rule
3), never re-implemented in a caller.

THIS IS A HEURISTIC, NOT A CLASSIFIER. `classify_risk` does case-insensitive
substring matching on a click target's text against small keyword lists. It has
false positives and false negatives. It is enough to make write actions stop for
a human in this MVP; it is not a safety guarantee.

Production direction: risk tier belongs as a per-capability / per-route
annotation that a HUMAN confirms or overrides when the capability is recorded --
the same review philosophy `agent/record.py` already uses for output extraction
(the recorder proposes structure; a person confirms what a capability returns).
Inferring the tier fresh from button text at replay time is the fallback, not
the design. Noted here as the natural extension; not built now.

Keyword choices (documented, debatable):
  * WRITE_KEYWORDS -> `confirm` tier: submit / process / confirm / create /
    delete / remove / "open sub-account". The words this target app and typical
    back-office UIs put on controls that mutate server state. "Process" is the
    sub-account form's submit button. "open sub-account" is the link that starts
    the write flow -- treated as a write even though it is navigation, because
    the goal context ("... and reach the confirmation screen") makes that path a
    mutation.
  * DESTRUCTIVE_KEYWORDS -> `blocked` tier: delete / remove. Never auto-executed,
    always escalates, regardless of AGENT_MAX_AUTO_RISK_TIER.
  * SENSITIVE_FIELD_KEYWORDS on a `type` action -> `blocked`, same category as a
    destructive click. The agent must never autofill a password / SSN / etc.:
    it only ever has MASKED context for such a field, so any value it proposes
    is a fabrication. A human must type it into the browser directly.
  * Everything else -> `safe`: `type` into a non-sensitive field, and clicks
    whose text matches no keyword ("Retrieve", "Cancel", "Back to lookup", ...).

`redact_type_value` and `SSN_SHAPE_RE` are the same flavour of keyword/pattern
heuristic for a related job: keeping secret values out of the step log
(redact_type_value) and out of the DOM string sent to the model
(surface.dom masking uses both). A real redactor (CLAUDE.md's /evidence module)
would supersede these.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from schema.action import Action
from schema.guardrail import GuardrailDecision, RiskTier

WRITE_KEYWORDS = (
    "submit", "process", "confirm", "create", "delete", "remove", "open sub-account",
)
DESTRUCTIVE_KEYWORDS = ("delete", "remove")

# The ONE shared sensitive-field keyword list. Used by:
#   * redact_type_value       -- keep a typed secret out of the step log
#   * classify_risk           -- a `type` into such a field is unconditionally blocked
#   * surface.dom masking     -- such a value is [MASKED] before the model ever sees it
# Substring, case-insensitive. MVP heuristic -- not a classifier.
SENSITIVE_FIELD_KEYWORDS = ("password", "passphrase", "ssn", "tax id", "pin")

# Second, independent net: an SSN-shaped run of digits anywhere in the cleaned
# text/attributes (inside a sentence, an attribute, wherever the label-adjacency
# check can't reach). Kept deliberately small; same MVP-heuristic spirit.
SSN_SHAPE_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

MAX_AUTO_TIER_ENV = "AGENT_MAX_AUTO_RISK_TIER"


def resolve_max_auto_tier() -> RiskTier:
    """The highest tier the agent may auto-execute, from AGENT_MAX_AUTO_RISK_TIER
    (default / unparseable -> safe)."""
    return RiskTier.parse(os.environ.get(MAX_AUTO_TIER_ENV, "safe"), default=RiskTier.SAFE)


def classify_risk(action: Action) -> RiskTier:
    """Risk tier for one action. See module docstring for the keyword rationale."""
    return _classify(action)[0]


def _classify(action: Action) -> tuple[RiskTier, str]:
    text = (getattr(action.target, "value", "") or "").lower()

    if action.action == "type":
        # A `type` into a sensitive field is unconditionally blocked -- same
        # category as a destructive click. The agent must never autofill secret
        # data: its proposed value is built from MASKED context, so it can only
        # be a guess (real testing saw it fabricate "000-00-0000").
        for kw in SENSITIVE_FIELD_KEYWORDS:
            if kw in text:
                return RiskTier.BLOCKED, (
                    f"type into a sensitive field ({kw!r} in {text!r}) -> blocked; "
                    "the agent must never autofill secret data"
                )
        return RiskTier.SAFE, "type into a non-sensitive field -> safe"

    if action.action != "click":
        return RiskTier.SAFE, f"{action.action} action -> safe (no mutation via typing)"

    for kw in DESTRUCTIVE_KEYWORDS:
        if kw in text:
            return RiskTier.BLOCKED, f"click text contains destructive keyword {kw!r} -> blocked"
    for kw in WRITE_KEYWORDS:
        if kw in text:
            return RiskTier.CONFIRM, f"click text contains write keyword {kw!r} -> confirm"
    return RiskTier.SAFE, "click text matches no write/destructive keyword -> safe"


def check_allowed(url: str, target_app_base_url: str) -> bool:
    """True iff `url` is within `target_app_base_url` (same scheme+host, path at
    or under the base path). An out-of-app URL is a categorical hard block."""
    if not url or not target_app_base_url:
        return False
    here, base = urlsplit(url), urlsplit(target_app_base_url)
    if (here.scheme.lower(), here.netloc.lower()) != (base.scheme.lower(), base.netloc.lower()):
        return False
    base_path = base.path.rstrip("/")
    if not base_path:
        return True
    return here.path == base_path or here.path.startswith(base_path + "/")


def evaluate(
    action: Action,
    current_url: str,
    target_app_base_url: str,
    max_auto_tier: RiskTier,
) -> GuardrailDecision:
    """Decide whether `action` on `current_url` may run without a human.

    An allowlist violation always wins: not allowed, regardless of tier.
    Otherwise: allowed iff tier is not `blocked` and tier <= max_auto_tier.
    """
    tier, tier_reason = _classify(action)

    if not check_allowed(current_url, target_app_base_url):
        return GuardrailDecision(
            allowed_automatically=False,
            tier=tier,
            allowlist_violation=True,
            reason=(
                f"allowlist violation: {current_url!r} is outside target app "
                f"{target_app_base_url!r} -- hard block regardless of tier"
            ),
        )

    if tier == RiskTier.BLOCKED:
        allowed = False
        reason = f"{tier_reason}; blocked tier never auto-executes -> escalate"
    elif tier <= max_auto_tier:
        allowed = True
        reason = f"{tier_reason}; within max auto tier ({max_auto_tier.value}) -> auto"
    else:
        allowed = False
        reason = f"{tier_reason}; above max auto tier ({max_auto_tier.value}) -> escalate"

    return GuardrailDecision(
        allowed_automatically=allowed,
        tier=tier,
        allowlist_violation=False,
        reason=reason,
    )


def redact_type_value(field_label: str, value: str) -> str:
    """"[REDACTED]" if `field_label` looks like a secret field, else `value`
    unchanged. Keyword heuristic (see module docstring); a real redactor supersedes."""
    label = (field_label or "").lower()
    if any(kw in label for kw in SENSITIVE_FIELD_KEYWORDS):
        return "[REDACTED]"
    return value
