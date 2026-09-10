"""`python -m agent.approve` -- a human approves a capability for unattended replay.

A recorded capability ships as `status: "draft"`. Replaying a draft via
`python -m replay` pauses for a human every run (the same pause/resume model as
a guardrail stop -- see `escalation.handoff.request_replay_approval`). This
command is the deliberate promotion to `status: "approved"`: it shows what the
capability does and its current replay-stability signal (derived live from any
`stability_report.json` files, not a stored score), then writes
`status="approved"` + an `ApprovalRecord` back to the file.

Idempotent: re-approving an already-approved capability is a clean no-op that
just reports the existing approval, not an error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from schema.capability import Capability
from schema.replay import StabilityReport, StabilitySignal

DEFAULT_EVIDENCE_DIR = "evidence/runs"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.approve",
        description="Approve a Capability artifact for unattended replay (draft -> approved).",
    )
    parser.add_argument("--capability", required=True, help="path to the capability JSON")
    parser.add_argument("--note", default=None,
                        help="optional reviewer note, stored in the approval record")
    parser.add_argument("--evidence-dir", default=DEFAULT_EVIDENCE_DIR,
                        help="where to look for stability_report.json files "
                        f"(default: {DEFAULT_EVIDENCE_DIR})")
    args = parser.parse_args(argv)

    path = Path(args.capability)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read capability file {args.capability!r}: {exc}", file=sys.stderr)
        return 1
    try:
        capability = Capability.model_validate_json(raw)
    except ValidationError as exc:
        print(
            f"invalid capability artifact {args.capability!r}: "
            f"{exc.error_count()} validation error(s) -- {_short(str(exc))}",
            file=sys.stderr,
        )
        return 1

    signal = StabilitySignal.from_reports(
        capability.capability_id, capability.version,
        _load_stability_reports(Path(args.evidence_dir)),
    )
    _print_summary(capability, signal)

    if capability.status == "approved":
        appr = capability.approval
        when = appr.approved_at.isoformat() if appr else "an unknown time"
        print(f"\nalready approved (at {when}) -- nothing to do.")
        if appr and appr.note:
            print(f"  note: {appr.note}")
        return 0

    approved = Capability.model_validate({
        **capability.model_dump(mode="json"),
        "status": "approved",
        "approval": {
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "note": args.note,
        },
    })
    path.write_text(approved.model_dump_json(indent=2) + "\n", encoding="utf-8")

    print(f"\napproved -- {args.capability} is now cleared for unattended replay.")
    print(f"  approved_at: {approved.approval.approved_at.isoformat()}")
    if args.note:
        print(f"  note:        {args.note}")
    return 0


# --- helpers ------------------------------------------------------------


def _load_stability_reports(evidence_dir: Path) -> list[StabilityReport]:
    """Every readable stability_report.json under `evidence_dir` (any depth).
    Unreadable / malformed files are skipped, not fatal -- this is a best-effort
    signal, not a gate."""
    reports: list[StabilityReport] = []
    if not evidence_dir.is_dir():
        return reports
    for report_path in sorted(evidence_dir.rglob("stability_report.json")):
        try:
            reports.append(
                StabilityReport.model_validate_json(report_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValidationError):
            continue
    return reports


def _print_summary(capability: Capability, signal: StabilitySignal) -> None:
    print(f"capability:   {capability.capability_id} v{capability.version}")
    print(f"name:         {capability.name}")
    print(f"about:        {capability.description}")
    print(f"status:       {capability.status}")
    print(f"target app:   {capability.target_app}")
    print("inputs:")
    for p in capability.parameters:
        print(f"  - {p.name}: {p.type}  (e.g. {p.example!r})  -- {p.description}")
    if not capability.parameters:
        print("  (none)")
    print("outputs:")
    for o in capability.outputs:
        print(f"  - {o.name}: {o.type}  <- {o.extraction.strategy} {o.extraction.target!r}")
    sc = capability.success_condition
    print(f"success when: {sc.strategy} {sc.target!r}")
    print(f"stability:    {signal.summary}")


def _short(text: str, limit: int = 300) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."


if __name__ == "__main__":
    sys.exit(main())
