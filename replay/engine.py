"""Deterministic replay: run a Capability artifact against the live page with NO
LLM in the loop, verify the success condition, return typed outputs, and name
exceptional states explicitly.

Same `act()` surface discovery used: `execute_on_page` for steps,
`resolve_locator` for the success-condition check and output extraction, and
`get_cleaned_dom` + `apply_synthetic_labels` before each step so a step that
references a synthesised label (e.g. "Member number") still resolves on the live
page -- label synthesis is structural, so no API call is needed at replay time.

Exceptional-state handling (not just the happy path):
  * params mismatch          -> failure, step_number=0, before any page touch
  * known business outcome    -> business_outcome (legitimate result; no output
                                 extraction is attempted)
  * transient step failure    -> one retry after a short wait, then hard failure
  * flow finished off-target   -> failure (success_condition did not resolve)
  * anything unexpected        -> failure (an absolute backstop; a ReplayResult
                                 is always returned, never a raw exception)

TODO(guardrail, CLAUDE.md rule 3): once /guardrail exists, re-check each action
against the shared allowlist + risk tier here before execute_on_page -- a
`blocked` action must refuse in replay exactly as in discovery.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from escalation.handoff import EscalationContext, request_escalation
from guardrail import policy as guardrail
from guardrail.policy import redact_type_value, resolve_max_auto_tier
from schema.action import ClickAction, TypeAction
from schema.guardrail import RiskTier
from schema.replay import BusinessOutcome, FailureDetail, ReplayResult
from surface.dom import apply_synthetic_labels, get_cleaned_dom
from surface.executor import SelectorResolutionError, execute_on_page, resolve_locator

try:  # importing the class does not launch a browser
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover
    class PlaywrightTimeoutError(Exception):
        pass

DEFAULT_RETRY_WAIT_S = 0.5
_RETRYABLE = (SelectorResolutionError, PlaywrightTimeoutError)
_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def default_run_id(prefix: str = "") -> str:
    """A run id that will not collide with a back-to-back run.

    A bare second-granularity UTC stamp (the old default) means two replays
    started in the same wall-clock second share a run dir and silently
    overwrite each other's result.json / steps.jsonl. Microseconds + a short
    random token make that practically impossible even across processes.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return f"{prefix}{stamp}_{secrets.token_hex(3)}"


@dataclass(frozen=True)
class BusinessSignature:
    """A page-text marker meaning the flow legitimately diverged."""

    contains: str  # case-insensitive substring expected in the page's visible text
    code: str
    message: str


# Demo-global table. PRODUCTION: this belongs ON the Capability artifact as a
# per-capability list -- a global table can't know which divergences matter for
# which flow, and markers collide across capabilities. Seeded with the target
# app's real cases: member 10004 -> "Access Restricted" page; an unknown member
# number -> "No Such Member" page. Markers are specific enough to skip the lookup
# page's own "10004 is restricted" hint text.
KNOWN_BUSINESS_OUTCOMES: tuple[BusinessSignature, ...] = (
    BusinessSignature(
        "access restricted", "restricted",
        "member is flagged restricted; lookup and account maintenance are denied",
    ),
    BusinessSignature(
        "no such member", "not_found",
        "member number is not on file",
    ),
)


def replay_capability(
    capability,
    params: dict,
    page,
    *,
    retry_wait: float = DEFAULT_RETRY_WAIT_S,
    on_step=None,
    max_auto_tier=None,
    run_id: str | None = None,
) -> ReplayResult:
    """Replay `capability` with `params` against `page`. Always returns a
    ReplayResult -- pydantic / Playwright exceptions never escape.

    Every step is run through the guardrail before execute_on_page; a step the
    guardrail won't auto-allow goes to `request_escalation` (pause, hand the
    live browser to a human, wait for resume/reject). Replay can require a
    human -- exactly the brief's "a replay hits a condition it can't recover
    from" case. On resume the blocked step is NOT executed; the flow's own
    business-outcome / success_condition checks then validate the end state.
    """
    if max_auto_tier is None:
        max_auto_tier = resolve_max_auto_tier()
    if run_id is None:
        run_id = default_run_id("replay-")
    if not isinstance(params, dict):
        # A non-dict `params` (e.g. a JSON list) would otherwise blow up deep in
        # the engine with a cryptic "dictionary update sequence" error. Turn it
        # into the same clean pre-flight failure a bad param name gets.
        return ReplayResult.failed(capability, {}, FailureDetail(
            step_number=0,
            expected="a params mapping of declared parameter names to values",
            observed=f"got {type(params).__name__}",
            message="params must be a dict; nothing was executed",
        ))
    try:
        return _replay(capability, params, page, retry_wait, on_step,
                       max_auto_tier, run_id)
    except Exception as exc:  # absolute backstop
        return ReplayResult.failed(capability, params, FailureDetail(
            step_number=-1,
            expected="run the recorded flow",
            observed=_short(f"{type(exc).__name__}: {exc}"),
            message="unexpected error inside the replay engine",
        ))


def _replay(capability, params, page, retry_wait, on_step, max_auto_tier, run_id) -> ReplayResult:
    bad = _validate_params(capability, params)
    if bad is not None:
        _emit(on_step, {"stage": "params", "status": "failed", "error": bad.message})
        return ReplayResult.failed(capability, params, bad)

    page.goto(capability.target_app)

    outcome = _match_business_outcome(page)
    if outcome is not None:
        _emit(on_step, {"stage": "landing", "check": "business_outcome", "code": outcome.code})
        return ReplayResult.business(capability, params, outcome)

    for step in capability.steps:
        _prepare_labels(page)
        action = _build_action(step, params)
        described = _describe(step, action)
        handoff = None

        # --- guardrail: gate every step before it can execute --------------
        decision = guardrail.evaluate(
            action, getattr(page, "url", None), capability.target_app, max_auto_tier
        )
        _emit(on_step, {"step_number": step.step_number,
                        "guardrail": decision.model_dump(mode="json")})
        if not decision.allowed_automatically:
            # a guardrail-blocked `type` = a sensitive field: the recorded value
            # (or a param) cannot be trusted here; a human must enter the real
            # one. request_escalation then never returns resume+intervened=False,
            # so it can never fall through to _execute_with_retry below.
            sensitive_type = action.action == "type" and decision.tier == RiskTier.BLOCKED
            handoff = request_escalation(page, decision, EscalationContext(
                run_id=run_id,
                capability_id_or_goal=capability.capability_id,
                step_number=step.step_number,
                action_summary=described,
                requires_human_value=sensitive_type,
            ))
            _emit(on_step, {"step_number": step.step_number,
                            "handoff": handoff.model_dump(mode="json")})
            if handoff.action == "reject":
                return ReplayResult.failed(capability, params, FailureDetail(
                    step_number=step.step_number,
                    expected=described,
                    observed=f"guardrail: {decision.reason}",
                    message=f"human rejected escalation at step {step.step_number}",
                ))
            if handoff.intervened:
                # the human completed the step themselves (page changed) -- skip
                # execution; re-check divergence now, success_condition validates
                # the end state after the loop.
                _emit(on_step, {"step_number": step.step_number, "status": "skipped",
                                "note": "human completed the step manually"})
                outcome = _match_business_outcome(page)
                if outcome is not None:
                    _emit(on_step, {"step_number": step.step_number,
                                    "check": "business_outcome", "code": outcome.code})
                    return ReplayResult.business(capability, params, outcome)
                continue
            # handoff.intervened is False: the human APPROVED this step as-is.
            # Fall through and execute the ORIGINAL action.
            assert not sensitive_type, (
                "a sensitive-field type must never reach execute via approval"
            )

        fail, attempts = _execute_with_retry(page, action, step, described, retry_wait)
        _emit(on_step, {
            "step_number": step.step_number,
            "action": step.action,
            "target": step.target.model_dump(),
            # brief 3.4: redact a typed secret before it hits the evidence log.
            "value": (redact_type_value(step.target.value, action.text)
                      if step.action == "type" else None),
            "attempts": attempts,
            "status": "failed" if fail else ("ok (human-approved)" if handoff else "ok"),
            "error": fail.observed if fail else None,
        })
        if fail is not None:
            return ReplayResult.failed(capability, params, fail)

        outcome = _match_business_outcome(page)
        if outcome is not None:
            _emit(on_step, {"step_number": step.step_number,
                            "check": "business_outcome", "code": outcome.code})
            return ReplayResult.business(capability, params, outcome)

    _prepare_labels(page)
    sc = capability.success_condition
    if not _resolves(page, sc.strategy, sc.target):
        _emit(on_step, {"check": "success_condition", "status": "failed"})
        return ReplayResult.failed(capability, params, FailureDetail(
            step_number=len(capability.steps),
            expected=f"final page satisfies success_condition ({sc.strategy} {sc.target!r})",
            observed="success_condition selector did not resolve on the final page",
            message="flow completed but did not reach the recorded end state",
        ))
    _emit(on_step, {"check": "success_condition", "status": "ok"})

    outputs, ofail = _extract_all(page, capability.outputs, len(capability.steps))
    if ofail is not None:
        _emit(on_step, {"check": "extraction", "status": "failed", "error": ofail.observed})
        return ReplayResult.failed(capability, params, ofail)
    _emit(on_step, {"check": "extraction", "status": "ok", "outputs": outputs})
    return ReplayResult.success(capability, params, outputs)


# --- steps --------------------------------------------------------------


def _execute_with_retry(page, action, step, described, retry_wait):
    """Run one step. Retry ONCE on a transient failure (selector miss / timeout)
    after `retry_wait`s; never more than once. Returns (FailureDetail|None, attempts)."""
    attempts = 0
    while True:
        attempts += 1
        try:
            execute_on_page(page, action)
            return None, attempts
        except _RETRYABLE as exc:
            if attempts >= 2:
                return _fail(step, described, exc, "still failing after one retry"), attempts
            time.sleep(retry_wait)
            _prepare_labels(page)  # transient slowness: re-sync labels, try once more
        except Exception as exc:  # non-retryable -> hard fail now, no retry
            return _fail(step, described, exc, f"raised {type(exc).__name__}"), attempts


def _build_action(step, params: dict):
    if step.action == "click":
        return ClickAction(action="click", target=step.target)
    text = _TEMPLATE_RE.sub(lambda m: str(params[m.group(1)]), step.value_template or "")
    return TypeAction(action="type", target=step.target, text=text)


def _prepare_labels(page) -> None:
    dom = get_cleaned_dom(page)
    apply_synthetic_labels(page, dom.synthetic_labels)


def _fail(step, described, exc, why) -> FailureDetail:
    return FailureDetail(
        step_number=step.step_number,
        expected=described,
        observed=_short(str(exc) or type(exc).__name__),
        message=f"step {step.step_number} {why}",
    )


def _describe(step, action) -> str:
    t = step.target
    if step.action == "type":
        return f"type {action.text!r} into {t.strategy}={t.value!r}"
    return f"click {t.strategy}={t.value!r}"


# --- params -----------------------------------------------------------


def _validate_params(capability, params: dict):
    declared = {p.name: p.type for p in capability.parameters}
    if set(params) != set(declared):
        missing = sorted(set(declared) - set(params))
        extra = sorted(set(params) - set(declared))
        bits = []
        if missing:
            bits.append(f"missing {missing}")
        if extra:
            bits.append(f"unexpected {extra}")
        return FailureDetail(
            step_number=0,
            expected=f"exactly the parameters {sorted(declared)}",
            observed=f"got {sorted(params)}" + ("; " + "; ".join(bits) if bits else ""),
            message="parameters do not match the capability's declared inputs",
        )
    for name, decl_type in declared.items():
        if not _valid_for_type(params[name], decl_type):
            return FailureDetail(
                step_number=0,
                expected=f"parameter {name!r} is a {decl_type}",
                observed=f"{name}={params[name]!r} ({type(params[name]).__name__})",
                message=f"parameter {name!r} is not a valid {decl_type}",
            )
    return None


def _valid_for_type(value, declared: str) -> bool:
    if isinstance(value, bool):
        return declared == "bool"
    if declared == "str":
        return isinstance(value, str)
    if declared == "int":
        if isinstance(value, int):
            return True
        return isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()) is not None
    if declared == "float":
        if isinstance(value, (int, float)):
            return True
        return (
            isinstance(value, str)
            and re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)", value.strip()) is not None
        )
    if declared == "bool":
        return isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0")
    return False


# --- final-page checks + extraction ---------------------------------


def _match_business_outcome(page):
    text = _visible_text(page).lower()
    for sig in KNOWN_BUSINESS_OUTCOMES:
        if sig.contains.lower() in text:
            return BusinessOutcome(code=sig.code, message=sig.message)
    return None


def _visible_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        try:
            return get_cleaned_dom(page).html
        except Exception:
            return ""


def _resolves(page, strategy: str, value: str) -> bool:
    try:
        return resolve_locator(page, strategy, value).count() > 0
    except Exception:
        return False


def _extract_all(page, output_specs, final_step: int):
    outputs: dict = {}
    for spec in output_specs:
        try:
            outputs[spec.name] = _extract_one(page, spec)
        except Exception as exc:
            return None, FailureDetail(
                step_number=final_step,
                expected=(
                    f"read output {spec.name!r} via "
                    f"{spec.extraction.strategy} {spec.extraction.target!r}"
                ),
                observed=_short(str(exc) or type(exc).__name__),
                message=f"success_condition held but output {spec.name!r} could not be read",
            )
    return outputs, None


def _extract_one(page, spec):
    located = resolve_locator(page, spec.extraction.strategy, spec.extraction.target).first
    if spec.extraction.strategy in ("text_contains", "text_of", "next_cell"):
        raw = located.inner_text()
    else:
        try:
            raw = located.input_value()
        except Exception:
            raw = located.inner_text()
    return _coerce((raw or "").strip(), spec.type)


def _coerce(text: str, declared: str):
    try:
        if declared == "int":
            return int(text)
        if declared == "float":
            return float(text)
        if declared == "bool":
            return text.strip().lower() in ("true", "1", "yes")
    except ValueError:
        return text
    return text


# --- misc -----------------------------------------------------------


def _emit(on_step, record: dict) -> None:
    if on_step is not None:
        on_step(record)


def _short(text, limit: int = 200) -> str:
    s = str(text).strip()
    first = s.splitlines()[0] if s else str(text)
    return first if len(first) <= limit else first[:limit] + "..."
