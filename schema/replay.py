"""The deterministic replay result contract -- a 3-way discriminated outcome.

Exactly one of `outputs` / `business_outcome` / `failure` is populated, keyed by
`status`:

  * success           -- the recorded flow ran to the end, the capability's
                         success_condition held, and `outputs` carries the
                         extracted, typed return values.
  * business_outcome   -- the UI worked correctly but reported a domain result
                         the caller must handle (member not found, restricted,
                         duplicate request, ...). A LEGITIMATE RESULT, not a
                         crash: replay stops, reports the code, and does NOT
                         attempt output extraction.
  * failure            -- a HARD STOP that needs debugging: a step could not run
                         as recorded even after one retry, or the flow finished
                         somewhere other than the recorded end state. The
                         artifact, the target app, or replay itself is wrong.

There is deliberately no `recoverable` status. A transient
SelectorResolutionError or Playwright timeout on a step is retried once inside
the engine after a short wait; if the retry succeeds, the condition never
surfaces here.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class BusinessOutcome(BaseModel):
    """An expected domain result the caller must branch on -- not an error."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Stable machine tag, e.g. 'not_found', 'restricted'.")
    message: str = Field(description="Human-readable context for the caller.")


class FailureDetail(BaseModel):
    """A hard stop -- something must be fixed before this replay can pass."""

    model_config = ConfigDict(extra="forbid")

    step_number: int = Field(
        description="1-based failing step. 0 = pre-flight parameter validation "
        "(before any page interaction). -1 = an unexpected error inside the engine, "
        "or a CLI-level setup failure (browser launch / navigation / sign-on) "
        "before the engine started.",
    )
    expected: str = Field(description="What the step was trying to do.")
    observed: str = Field(
        description="What actually happened / was on the page -- an error message "
        "or a short snippet, never a full DOM dump.",
    )
    message: str = Field(description="One-line summary of the failure.")


class ReplayResult(BaseModel):
    """Outcome of one deterministic replay. See the module docstring for what
    each `status` means and which field it populates."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "business_outcome", "failure"] = Field(
        description="Which of the three outcomes occurred.",
    )
    capability_id: str = Field(description="Capability that was replayed.")
    version: str = Field(description="Capability version that was replayed.")
    params: dict[str, Any] = Field(
        description="The parameters this replay ran with, echoed back verbatim.",
    )
    outputs: Optional[dict[str, Any]] = Field(
        default=None,
        description="Extracted, typed return values. Populated only on status='success'.",
    )
    business_outcome: Optional[BusinessOutcome] = Field(
        default=None,
        description="Populated only on status='business_outcome'.",
    )
    failure: Optional[FailureDetail] = Field(
        default=None,
        description="Populated only on status='failure'.",
    )

    # Constructors -- `capability` is any object exposing `.capability_id` / `.version`.

    @classmethod
    def success(cls, capability, params: dict, outputs: dict) -> "ReplayResult":
        return cls(
            status="success", capability_id=capability.capability_id,
            version=capability.version, params=dict(params), outputs=outputs,
        )

    @classmethod
    def business(cls, capability, params: dict, outcome: BusinessOutcome) -> "ReplayResult":
        return cls(
            status="business_outcome", capability_id=capability.capability_id,
            version=capability.version, params=dict(params), business_outcome=outcome,
        )

    @classmethod
    def failed(cls, capability, params: dict, detail: FailureDetail) -> "ReplayResult":
        return cls(
            status="failure", capability_id=capability.capability_id,
            version=capability.version, params=dict(params), failure=detail,
        )
