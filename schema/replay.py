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


# --- multi-run stability (brief section 8 stretch goal) -------------------
#
# A thin aggregate over N repeated `replay_capability` calls with the SAME
# capability + params. It does not change replay -- it only asks "did N
# identical invocations produce identical results?". The headline signal is
# `all_outputs_identical`: if replay is deterministic (the whole point of the
# phase) every successful run must extract byte-identical outputs.


class DurationStats(BaseModel):
    """Wall-clock spread of the N runs, in seconds."""

    model_config = ConfigDict(extra="forbid")

    min: float
    max: float
    mean: float


class RunSummary(BaseModel):
    """One run in the batch, condensed for a human skimming the report."""

    model_config = ConfigDict(extra="forbid")

    run: int = Field(description="1-based index in the batch.")
    status: Literal["success", "business_outcome", "failure"]
    key_output: Optional[str] = Field(
        default=None,
        description="The salient value for a skim: 'name=value[, ...]' for "
        "success, the business-outcome code, or 'step N: message' for failure.",
    )
    duration_seconds: float


class StabilityReport(BaseModel):
    """Aggregate of N replays of one capability with one set of params."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    version: str
    params: dict[str, Any]
    total_runs: int
    status_counts: dict[str, int] = Field(
        description="Count per status. Always carries all three keys "
        "(success / business_outcome / failure), zero-filled.",
    )
    successful_runs: int = Field(
        description="Runs with status=success -- the population "
        "`all_outputs_identical` is computed over.",
    )
    all_outputs_identical: bool = Field(
        description="True iff every status=success run extracted exactly the "
        "same `outputs`. Vacuously True with 0 or 1 successful runs. False here "
        "is a REAL finding: replay is supposed to be deterministic.",
    )
    duration_seconds: DurationStats
    per_run: list[RunSummary]

    @classmethod
    def from_runs(cls, capability, params: dict,
                  runs: list[tuple["ReplayResult", float]]) -> "StabilityReport":
        """Build the report from `(result, duration_seconds)` pairs, in run order."""
        if not runs:
            raise ValueError("a stability report needs at least one run")

        results = [r for r, _ in runs]
        durations = [d for _, d in runs]

        status_counts = {"success": 0, "business_outcome": 0, "failure": 0}
        for r in results:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1

        success_outputs = [r.outputs for r in results if r.status == "success"]
        all_identical = all(o == success_outputs[0] for o in success_outputs)

        per_run = [
            RunSummary(run=i, status=r.status, key_output=_key_output(r),
                       duration_seconds=round(d, 4))
            for i, (r, d) in enumerate(runs, start=1)
        ]

        return cls(
            capability_id=capability.capability_id,
            version=capability.version,
            params=dict(params),
            total_runs=len(runs),
            status_counts=status_counts,
            successful_runs=status_counts["success"],
            all_outputs_identical=all_identical,
            duration_seconds=DurationStats(
                min=round(min(durations), 4),
                max=round(max(durations), 4),
                mean=round(sum(durations) / len(durations), 4),
            ),
            per_run=per_run,
        )


def _key_output(result: "ReplayResult") -> Optional[str]:
    if result.status == "success":
        items = (result.outputs or {}).items()
        return ", ".join(f"{k}={v!r}" for k, v in items) or None
    if result.status == "business_outcome" and result.business_outcome is not None:
        return result.business_outcome.code
    if result.status == "failure" and result.failure is not None:
        return f"step {result.failure.step_number}: {result.failure.message}"
    return None
