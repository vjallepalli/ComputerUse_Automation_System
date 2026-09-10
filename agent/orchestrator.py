"""Discovery run: wire observe -> decide -> act -> record into one driven loop.

    python run.py --goal "Look up member 10003 and read their savings balance"

Per iteration:
  observe   dom = get_cleaned_dom(page); apply_synthetic_labels(page, dom.pairs)
  decide    action = ask_claude(goal, dom.html, history)
  record    append the step to artifacts/runs/<run-id>/steps.jsonl  (before acting)
  act       execute_on_page(page, action)  (skipped for `done`)

The loop stops on: a DoneAction (exit 0), the max-step limit (exit 1), an
AgentActionError from decide, or a SelectorResolutionError from act (exit 1).
Failures are recorded as a step and summarised on stderr -- never a bare
traceback, never silently swallowed-and-continued.

--- steps.jsonl schema (one JSON object per line) --------------------------------
{
  "step":    1,                       int, 1-based
  "ts":      "2026-09-09T12:34:56.789+00:00",   ISO-8601 UTC, when recorded
  "url":     "http://127.0.0.1:5001/member",    page URL at observe time (or null)
  "dom": {                            the cleaned DOM the model saw, offloaded:
    "sha256": "9f86d0...",            hex digest of the exact bytes
    "chars":  472,
    "path":   "dom/step-001.html"     relative to the run dir; full text lives here
  },
  "synthetic_labels": [               pairs pushed onto the live DOM this step
    {"selector": "input[name=\"q\"]", "match_index": 0, "label": "Member number"}
  ],
  "decision": {                       the parsed Action (Action.model_dump()), or null
    "action": "type",
    "target": {"strategy": "label", "value": "Member number"},
    "text":   "10003"                 REDACTED to "[REDACTED]" if the field label
                                      looks sensitive (guardrail.redact_type_value)
  },
  "guardrail": {                       the GuardrailDecision for this action, or null
    "allowed_automatically": true, "tier": "safe",
    "allowlist_violation": false, "reason": "..."
  },
  "handoff":  {"action": "resume", "intervened": true, ...} or null (human handoff)
  "outcome":  {"status": "ok", "action": "type", ...},   execute_on_page result,
                                      {"status":"done"/"resumed"/...} or null
  "error":    {"type": "SelectorResolutionError", "message": "..."} or null
}

Every action that reaches the page has a non-null `guardrail` entry -- the
evidence shows every action was checked, not only the ones that got blocked.

The DOM is offloaded to a sidecar file (not inlined) so the log stays greppable
and small per line; the sha256 lets replay assert DOM identity without diffing
full text. `meta.json` in the run dir carries goal / model / start_url / result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from agent.decide import AgentActionError, ask_claude, resolve_model
from escalation.handoff import EscalationContext, request_escalation
from guardrail import policy as guardrail
from guardrail.policy import redact_type_value, resolve_max_auto_tier
from schema.action import DoneAction
from schema.guardrail import RiskTier
from surface.dom import apply_synthetic_labels, get_cleaned_dom
from surface.executor import SelectorResolutionError, execute_on_page

DEFAULT_MAX_STEPS = 25
DEFAULT_BASE_URL = "http://127.0.0.1:5001"

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_STARTUP = 2

# What a `type` step's value becomes in the log when a human handled it during
# escalation: the agent did NOT type this -- a human entered a value directly,
# off to the side, that the agent (by design) never saw. Recording the agent's
# fabricated placeholder, even redacted, would misrepresent what happened.
_MANUAL_INTERVENTION_TEXT = "[not executed by agent; human entered a value directly]"


def _is_manual_intervention(handoff) -> bool:
    """The escalation ended with the human doing the step themselves (page changed)
    -- so the agent's proposed action/value was not executed."""
    return (
        handoff is not None
        and getattr(handoff, "action", None) == "resume"
        and getattr(handoff, "intervened", False)
    )


# --- step log ---------------------------------------------------------------


class StepLogger:
    """Appends one JSON line per step to <run_dir>/steps.jsonl, DOM to a sidecar."""

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.dom_dir = self.run_dir / "dom"
        self.dom_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "steps.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, *, step, url, cleaned, decision, outcome, error,
            guardrail=None, handoff=None) -> dict:
        dom_html = getattr(cleaned, "html", "") or ""
        dom_file = self.dom_dir / f"step-{step:03d}.html"
        dom_file.write_text(dom_html, encoding="utf-8")

        decision_dump = None
        if decision is not None:
            decision_dump = decision.model_dump()
            # brief 3.4: never persist a typed secret. Every path a `type`
            # value could be written goes through here -- auto-executed,
            # human-approved-and-executed, or skipped-because-human-did-it.
            if decision_dump.get("action") == "type":
                if _is_manual_intervention(handoff):
                    decision_dump["text"] = _MANUAL_INTERVENTION_TEXT
                else:
                    decision_dump["text"] = redact_type_value(
                        decision.target.value, decision_dump.get("text", "")
                    )

        record = {
            "step": step,
            "ts": datetime.now(timezone.utc).isoformat(),
            "url": url,
            "dom": {
                "sha256": hashlib.sha256(dom_html.encode("utf-8")).hexdigest(),
                "chars": len(dom_html),
                "path": str(dom_file.relative_to(self.run_dir)),
            },
            "synthetic_labels": list(getattr(cleaned, "synthetic_labels", []) or []),
            "decision": decision_dump,
            "guardrail": guardrail.model_dump(mode="json") if guardrail is not None else None,
            "handoff": handoff.model_dump(mode="json") if handoff is not None else None,
            "outcome": outcome,
            "error": (
                {"type": type(error).__name__, "message": str(error)}
                if error is not None else None
            ),
        }
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()
        return record

    def write_meta(self, meta: dict) -> None:
        (self.run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )

    def close(self) -> None:
        self._fh.close()


# --- the loop --------------------------------------------------------------


def run_loop(
    goal: str,
    page,
    logger: StepLogger,
    *,
    max_steps: int,
    target_app: str,
    max_auto_tier: RiskTier | None = None,
    client=None,
) -> tuple[int, str]:
    """Drive the page toward `goal`. Returns (exit_code, human-readable reason).

    Every candidate action is run through the guardrail *before* execute_on_page.
    An action the guardrail won't auto-allow goes to `request_escalation` (pause,
    hand the live browser to the human, wait for resume/reject). On resume the
    loop does NOT execute that action -- it records the handoff and re-observes /
    re-decides from the (possibly human-changed) page on the next iteration.
    """
    if max_auto_tier is None:
        max_auto_tier = resolve_max_auto_tier()
    history: list = []

    for step in range(1, max_steps + 1):
        cleaned = get_cleaned_dom(page)
        apply_synthetic_labels(page, cleaned.synthetic_labels)
        url = getattr(page, "url", None)

        try:
            action = ask_claude(goal, cleaned.html, history, client=client)
        except AgentActionError as exc:
            logger.log(step=step, url=url, cleaned=cleaned,
                       decision=None, outcome=None, error=exc)
            print(_console_line(step, None, None, exc))
            return EXIT_INCOMPLETE, f"AgentActionError at step {step}: {_first_line(exc)}"

        if isinstance(action, DoneAction):
            outcome = {"status": "done", "reason": action.reason}
            logger.log(step=step, url=url, cleaned=cleaned,
                       decision=action, outcome=outcome, error=None)
            print(_console_line(step, action, outcome, None))
            return EXIT_OK, f"done at step {step}: {action.reason}"

        # --- guardrail: gate every page action before it can execute ---------
        decision = guardrail.evaluate(action, url, target_app, max_auto_tier)
        handoff = None
        if not decision.allowed_automatically:
            print(f"{_console_line(step, action, None, None)}  "
                  f"[guardrail: {decision.tier.value} -> escalate]")
            # a guardrail-blocked `type` = a sensitive field; the agent's proposed
            # value is built from MASKED context and cannot be trusted -- a human
            # must enter it. request_escalation then never returns resume+
            # intervened=False for this case, so it can never reach execute below.
            sensitive_type = action.action == "type" and decision.tier == RiskTier.BLOCKED
            handoff = request_escalation(page, decision, EscalationContext(
                run_id=logger.run_dir.name,
                capability_id_or_goal=goal,
                step_number=step,
                action_summary=_verb(action),
                requires_human_value=sensitive_type,
            ))
            if handoff.action == "reject":
                logger.log(step=step, url=url, cleaned=cleaned, decision=action,
                           outcome=None, error=None, guardrail=decision, handoff=handoff)
                print(f"[{step}] human rejected escalation")
                return EXIT_INCOMPLETE, f"human rejected escalation at step {step}"
            if handoff.intervened:
                # the human completed the step themselves (page changed) -- skip
                # execution and re-decide from the new state on the next iteration.
                # The agent's proposed value is NOT logged (it was never used, and
                # for a sensitive field it's a fabrication that reads as a leak).
                logger.log(step=step, url=url, cleaned=cleaned, decision=action,
                           outcome={"status": "resumed", "intervened": True,
                                    "note": "human completed the step manually; "
                                            "agent's proposed value was not used or seen"},
                           error=None, guardrail=decision, handoff=handoff)
                print(_console_line(step, action, {"status": "resumed"}, None, handoff=handoff))
                history.append(
                    f"[step {step}] guardrail stopped {_verb(action)}; a human handled "
                    f"it manually in the browser -- the agent did not act and does not "
                    f"know what value was entered; do not retry this field"
                )
                continue
            # handoff.intervened is False: the human APPROVED this action as-is.
            # Fall through and execute the ORIGINAL action, then continue normally.
            assert not sensitive_type, (
                "a sensitive-field type must never reach execute_on_page via approval"
            )

        try:
            result = execute_on_page(page, action)
        except SelectorResolutionError as exc:
            logger.log(step=step, url=url, cleaned=cleaned, decision=action,
                       outcome=None, error=exc, guardrail=decision, handoff=handoff)
            print(_console_line(step, action, None, exc, handoff=handoff))
            return EXIT_INCOMPLETE, (
                f"SelectorResolutionError at step {step}: {_first_line(exc)}"
            )
        except Exception as exc:  # unexpected -- record the step, then surface it
            logger.log(step=step, url=url, cleaned=cleaned, decision=action,
                       outcome=None, error=exc, guardrail=decision, handoff=handoff)
            print(_console_line(step, action, None, exc, handoff=handoff))
            raise

        if handoff is not None:  # approved-by-human execution (intervened=False)
            result = {**result, "note": "approved by human; executed as proposed"}
        logger.log(step=step, url=url, cleaned=cleaned, decision=action,
                   outcome=result, error=None, guardrail=decision, handoff=handoff)
        print(_console_line(step, action, result, None, handoff=handoff))
        history.append(result)

    return EXIT_INCOMPLETE, f"reached max steps ({max_steps}) without a done action"


def _console_line(step, decision, outcome, error, handoff=None) -> str:
    tag = f"[{step}]"
    if error is not None:
        what = _verb(decision) if decision is not None else "decide"
        return f"{tag} {what} -> ERROR {type(error).__name__}: {_first_line(error)}"
    if decision is None:
        return f"{tag} (no decision)"
    if decision.action == "done":
        return f"{tag} done: {decision.reason}"
    status = (outcome or {}).get("status", "?")
    if _is_manual_intervention(handoff):
        return (f"{tag} human handled {decision.target.value!r} manually; "
                f"agent's proposed value not used -> {status}")
    if decision.action == "type":
        shown = redact_type_value(decision.target.value, decision.text)
        return f"{tag} type {shown!r} into {decision.target.value!r} -> {status}"
    if decision.action == "click":
        return f"{tag} click {decision.target.value!r} -> {status}"
    return f"{tag} {decision.action} -> {status}"


def _verb(decision) -> str:
    if decision is None:
        return "decide"
    if decision.action == "type":
        return f"type into {decision.target.value!r}"
    if decision.action == "click":
        return f"click {decision.target.value!r}"
    return decision.action


def _first_line(exc) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


# --- browser wiring ------------------------------------------------------


def run_discovery(
    goal: str,
    *,
    start_url: str,
    run_dir,
    max_steps: int = DEFAULT_MAX_STEPS,
    headed: bool = False,
    client=None,
) -> int:
    from playwright.sync_api import sync_playwright

    logger = StepLogger(run_dir)
    parts = urlsplit(start_url)
    target_app = f"{parts.scheme}://{parts.netloc}"
    max_auto_tier = resolve_max_auto_tier()
    print(f"discovery run -> {logger.run_dir}")
    print(f"goal: {goal}")
    print(f"guardrail: max auto risk tier = {max_auto_tier.value}  (target app {target_app})")
    started = datetime.now(timezone.utc).isoformat()
    code, reason = EXIT_INCOMPLETE, "did not start"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not headed)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(start_url)
                _sign_on_if_needed(page, start_url)
                code, reason = run_loop(
                    goal, page, logger, max_steps=max_steps, client=client,
                    target_app=target_app, max_auto_tier=max_auto_tier,
                )
            finally:
                context.close()
                browser.close()
    finally:
        logger.write_meta({
            "run_id": logger.run_dir.name,
            "goal": goal,
            "model": resolve_model(),
            "start_url": start_url,
            "target_app": target_app,
            "max_auto_risk_tier": max_auto_tier.value,
            "max_steps": max_steps,
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": code,
            "stop_reason": reason,
        })
        logger.close()

    print(f"\n{'completed' if code == EXIT_OK else 'stopped'}: {reason}", file=sys.stderr)
    return code


def _sign_on_if_needed(page, start_url: str) -> None:
    """Programmatic sign-on if the app bounced us to the login form.

    Convenience only -- credentials aren't part of the capability being
    discovered. Uses TARGET_APP_USERNAME / TARGET_APP_PASSWORD, default
    clerk / vault (the target_app demo operator).
    """
    if not page.locator("input[name='u']").count():
        return
    import os

    user = os.environ.get("TARGET_APP_USERNAME") or "clerk"
    secret = os.environ.get("TARGET_APP_PASSWORD") or "vault"
    page.fill("input[name='u']", user)
    page.fill("input[name='p']", secret)
    page.click("input[value='Sign On']")
    try:
        page.wait_for_url("**/member", timeout=5000)
    except Exception:
        pass
    if page.url.rstrip("/") != start_url.rstrip("/"):
        page.goto(start_url)
    print(f"signed on as {user}")


# --- CLI ----------------------------------------------------------------


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main(argv=None) -> int:
    import os

    parser = argparse.ArgumentParser(
        prog="run.py", description="Run one discovery run against the target app."
    )
    parser.add_argument("--goal", required=True, help="what the agent should accomplish")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--start-url", default=None,
                        help="default: $TARGET_APP_BASE_URL/member")
    parser.add_argument("--run-id", default=None, help="default: UTC timestamp")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args(argv)

    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set - add it to .env", file=sys.stderr)
        return EXIT_STARTUP

    base = os.environ.get("TARGET_APP_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    start_url = args.start_url or f"{base}/member"
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path("artifacts") / "runs" / run_id

    return run_discovery(
        args.goal,
        start_url=start_url,
        run_dir=run_dir,
        max_steps=args.max_steps,
        headed=args.headed,
    )
