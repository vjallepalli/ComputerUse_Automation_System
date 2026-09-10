"""Escalation types: the request written when a run pauses for a human, and the
result of the human's decision.

`EscalationRequest` references a saved DOM snapshot by path (same sidecar
pattern as steps.jsonl's `dom.path`) rather than inlining the markup.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from schema.guardrail import RiskTier


class EscalationRequest(BaseModel):
    """One 'a human needs to look at this' event, persisted as JSON."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(description="The discovery/replay run this happened in.")
    capability_id_or_goal: str = Field(
        description="The capability id (replay) or the discovery goal (discovery) "
        "that led here.",
    )
    step_number: int = Field(description="Step at which the run paused.")
    tier: RiskTier = Field(description="Risk tier that triggered the pause.")
    reason: str = Field(description="The guardrail's reason string.")
    current_url: Optional[str] = Field(description="Page URL at the moment of the pause.")
    dom_snapshot_path: Optional[str] = Field(
        default=None,
        description="Path (relative to the request file) to the saved cleaned-DOM "
        "snapshot of the live page at pause time. Not inlined. None for a "
        "pre-replay approval gate, where there is no live page yet.",
    )
    screenshot_path: Optional[str] = Field(
        default=None,
        description="Path to a screenshot of the live page, if one could be taken.",
    )
    timestamp: str = Field(description="ISO-8601 UTC, when the pause happened.")


class HandoffResult(BaseModel):
    """What the human decided, and whether the page changed while they had control."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["resume", "reject"] = Field(
        description="'resume' = continue the run from the CURRENT page state. "
        "'reject' = stop the run.",
    )
    intervened: bool = Field(
        default=False,
        description="True if the page (URL or cleaned DOM) changed between pause "
        "and resume -- i.e. the human operated the browser directly.",
    )
    url_before: Optional[str] = Field(default=None, description="URL when the run paused.")
    url_after: Optional[str] = Field(default=None, description="URL when the human resumed.")
    dom_snapshot_after_path: Optional[str] = Field(
        default=None,
        description="Path to the post-handoff DOM snapshot, if the page changed.",
    )
    note: Optional[str] = Field(default=None, description="Free-text summary for the log.")
