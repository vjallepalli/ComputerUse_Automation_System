"""Guardrail types: the three risk tiers and the decision the policy returns.

`RiskTier` orders by *risk*, not by string: safe < confirm < blocked. It is a
str-Enum so it serialises to "safe" / "confirm" / "blocked" in JSON evidence,
but its comparison operators are overridden to use the risk rank -- otherwise
`"blocked" < "confirm"` would be true lexicographically, which is backwards.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

_RANK = {"safe": 0, "confirm": 1, "blocked": 2}


class RiskTier(str, Enum):
    SAFE = "safe"
    CONFIRM = "confirm"       # a write / mutation -- needs control-owner approval
    BLOCKED = "blocked"       # never auto-executed; always escalates

    @property
    def rank(self) -> int:
        return _RANK[self.value]

    def __lt__(self, other):
        return self.rank < other.rank if isinstance(other, RiskTier) else NotImplemented

    def __le__(self, other):
        return self.rank <= other.rank if isinstance(other, RiskTier) else NotImplemented

    def __gt__(self, other):
        return self.rank > other.rank if isinstance(other, RiskTier) else NotImplemented

    def __ge__(self, other):
        return self.rank >= other.rank if isinstance(other, RiskTier) else NotImplemented

    @classmethod
    def parse(cls, value: str, default: "RiskTier" = None) -> "RiskTier":
        try:
            return cls((value or "").strip().lower())
        except ValueError:
            return default if default is not None else cls.SAFE


class GuardrailDecision(BaseModel):
    """The guardrail's verdict on one action at one moment."""

    model_config = ConfigDict(extra="forbid")

    allowed_automatically: bool = Field(
        description="True = execute now, no human. False = must go through "
        "/escalation (human resume/reject) before anything runs.",
    )
    tier: RiskTier = Field(description="Resolved risk tier of the action.")
    reason: str = Field(
        description="Why this tier / verdict -- which keyword matched, the "
        "max-auto ceiling, or the allowlist host mismatch.",
    )
    allowlist_violation: bool = Field(
        description="True = the action's page is outside the run's target_app. "
        "A categorical hard block that overrides the tier entirely.",
    )
