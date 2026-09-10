"""Human handoff: real control transfer in the SAME session.

MOCKED vs REAL (per the brief's scope note):
  * REAL -- the pause, the control transfer, and the resume. When the guardrail
    blocks an action the run STOPS, the already-open (`--headed`) browser window
    is handed to the human to drive directly, and the run continues only after
    the human types `resume` (or `reject`) in this same terminal. Same browser
    context, same page, same cookies -- not a fresh session.
  * MOCKED -- the "operator console" is THIS terminal plus THAT browser window.
    There is deliberately no separate operator web UI (explicitly out of scope
    per the brief). `input()` on stdin is the approval channel.

`request_escalation` writes an `EscalationRequest` (+ a cleaned-DOM snapshot,
+ a screenshot if it can) under `escalation/requests/<run-id>/`, prints what the
agent wanted and why it stopped, then blocks on `input()`.

`resume` has TWO meanings for a NORMAL escalation, and the caller branches on
`HandoffResult.intervened`:
  * intervened=False -- the human did NOT touch the browser; they APPROVE the
    agent's proposed action. The caller executes that original action.
  * intervened=True  -- the page changed while the human had control (they did
    the step by hand). The caller SKIPS the proposed action and re-evaluates.
Conflating the two makes an approved action never run and re-escalate forever.

For a SENSITIVE-value escalation (`EscalationContext.requires_human_value`) the
"approve as-is" meaning does NOT exist: the agent only ever had MASKED context
for the field, so any value it proposes is a fabrication (real testing saw it
invent "000-00-0000"). `resume` is honoured ONLY once the human has actually
entered a value into the browser; a bare `resume` re-prints the reason and waits
again. It never silently executes and never loops unboundedly -- `reject` always
exits.

"Changed" for both cases = URL changed, cleaned-DOM changed, OR any live field
value changed. The last is essential for the sensitive case: the masked DOM
looks identical before and after the human types the real SSN.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schema.escalation import EscalationRequest, HandoffResult
from schema.guardrail import GuardrailDecision
from surface.dom import get_cleaned_dom, read_field_values

REQUESTS_ROOT = Path("escalation/requests")

_SENSITIVE_REPROMPT = (
    "  No value entered yet. This field needs a real secret the agent must not\n"
    "  see -- type it into the field IN THE BROWSER yourself, then 'resume'\n"
    "  (or 'reject' to stop)."
)


@dataclass
class EscalationContext:
    run_id: str
    capability_id_or_goal: str
    step_number: int
    action_summary: str                      # e.g. "click 'Process'"
    requires_human_value: bool = False       # a guardrail-blocked sensitive `type`
    out_root: Optional[Path] = None          # default: escalation/requests/


def request_escalation(
    page,
    decision: GuardrailDecision,
    context: EscalationContext,
    *,
    input_fn=input,
) -> HandoffResult:
    """Persist the request, print instructions, block for a human, return their call."""
    now = datetime.now(timezone.utc)
    slug = now.strftime("%Y%m%dT%H%M%S_%f") + "Z"
    req_dir = (context.out_root or REQUESTS_ROOT) / context.run_id
    req_dir.mkdir(parents=True, exist_ok=True)

    url_before = getattr(page, "url", None)
    dom_before = _capture_dom(page, req_dir / f"{slug}.dom.html")
    raw_before = read_field_values(page)
    shot = _capture_screenshot(page, req_dir / f"{slug}.png")

    request = EscalationRequest(
        run_id=context.run_id,
        capability_id_or_goal=context.capability_id_or_goal,
        step_number=context.step_number,
        tier=decision.tier,
        reason=decision.reason,
        current_url=url_before,
        dom_snapshot_path=f"{slug}.dom.html",
        screenshot_path=(f"{slug}.png" if shot else None),
        timestamp=now.isoformat(),
    )
    (req_dir / f"{slug}.json").write_text(
        request.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _print_instructions(request, context, req_dir / f"{slug}.json")

    while True:
        answer = _prompt(input_fn)
        url_after = getattr(page, "url", None)
        dom_after = _capture_dom(page, req_dir / f"{slug}.after.dom.html")
        raw_after = read_field_values(page)
        changed = _changed(url_before, url_after, dom_before, dom_after,
                           raw_before, raw_after)

        if answer == "reject":
            note = (
                "human rejected; the required sensitive value was not provided"
                if context.requires_human_value
                else "human rejected the escalation"
            )
            return _result("reject", changed, url_before, url_after, slug, note)

        # answer == "resume"
        if context.requires_human_value and not changed:
            # bare approval is not allowed for a sensitive field -- wait for a
            # real value. Not silent (re-prints the reason), not unbounded
            # ('reject' or EOF exits _prompt).
            print(_SENSITIVE_REPROMPT)
            continue

        if context.requires_human_value:
            note = "human entered the sensitive value directly; agent will re-observe"
        elif changed:
            note = "human completed the step manually before resuming (page changed)"
        else:
            note = "human approved the proposed action as-is (page untouched)"
        return _result("resume", changed, url_before, url_after, slug, note)


# --- internals --------------------------------------------------------------


def _result(action, changed, url_before, url_after, slug, note) -> HandoffResult:
    return HandoffResult(
        action=action,
        intervened=changed,
        url_before=url_before,
        url_after=url_after,
        dom_snapshot_after_path=(f"{slug}.after.dom.html" if changed else None),
        note=note,
    )


def _changed(u0, u1, dom0, dom1, raw0, raw1) -> bool:
    return u0 != u1 or _digest(dom0) != _digest(dom1) or raw0 != raw1


def _prompt(input_fn) -> str:
    while True:
        try:
            answer = (input_fn("guardrail> ") or "").strip().lower()
        except (EOFError, StopIteration):
            return "reject"
        if answer in ("resume", "reject"):
            return answer
        print("  please type 'resume' or 'reject'")


def _print_instructions(request: EscalationRequest, ctx: EscalationContext, req_path: Path) -> None:
    line = "=" * 64
    header = (
        f"\n{line}\n"
        f"GUARDRAIL STOP -- human decision needed\n"
        f"  run:             {request.run_id}\n"
        f"  step:            {request.step_number}\n"
        f"  capability/goal: {request.capability_id_or_goal}\n"
        f"  the agent wants: {ctx.action_summary}\n"
        f"  risk tier:       {request.tier.value}"
        f"{'  (ALLOWLIST VIOLATION)' if 'allowlist violation' in request.reason else ''}\n"
        f"  why stopped:     {request.reason}\n"
        f"  page:            {request.current_url}\n"
        f"  saved:           {req_path}\n\n"
        f"The SAME browser window (already open) is yours now.\n\n"
    )
    if ctx.requires_human_value:
        body = (
            f"  This field needs sensitive data the agent CANNOT see. Its proposed\n"
            f"  value was built from MASKED context -- approving it would enter a\n"
            f"  fabricated value. There is no 'just approve it' for this case.\n\n"
            f"  Enter the real value into the field IN THE BROWSER yourself, then\n"
            f"  type 'resume'. Typing 'resume' without entering a value will just\n"
            f"  ask again. Type 'reject' to stop the run.\n"
        )
    else:
        body = (
            f"  type 'resume' to APPROVE this action -- the agent executes it as proposed.\n"
            f"       OR: do the step yourself in the browser first, then type 'resume'\n"
            f"       to continue from the resulting page (the agent skips its action).\n"
            f"  type 'reject' to stop the run.\n"
        )
    print(header + body + line)


def _capture_dom(page, dest: Path) -> str:
    try:
        html = get_cleaned_dom(page).html
    except Exception:
        html = ""
    try:
        dest.write_text(html, encoding="utf-8")
    except Exception:
        pass
    return html


def _capture_screenshot(page, dest: Path) -> bool:
    try:
        page.screenshot(path=str(dest))
        return True
    except Exception:
        return False


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
