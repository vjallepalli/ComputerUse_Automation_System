"""The guardrail is genuinely in the path: a risky action escalates instead of
executing, in BOTH loops. execute_on_page / page / escalation are mocked.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import orchestrator
from agent.orchestrator import EXIT_INCOMPLETE, EXIT_OK, StepLogger, run_loop
from replay import engine
from replay.engine import replay_capability
from schema.action import ClickAction, DoneAction, Target, TypeAction
from schema.capability import (
    Capability, CapabilityStep, Extraction, OutputSpec, Parameter, SuccessCondition,
)
from schema.escalation import HandoffResult
from schema.guardrail import RiskTier

APP = "http://127.0.0.1:5001"
PAGE = SimpleNamespace(url=f"{APP}/member/10003/sub-account")


def _steps(run_dir):
    return [json.loads(x) for x in (run_dir / "steps.jsonl").read_text().splitlines()]


def _risky_click():
    return ClickAction(action="click", target=Target(strategy="text_contains", value="Process"))


def _safe_type(value="Member number", text="10003"):
    return TypeAction(action="type", target=Target(strategy="label", value=value), text=text)


# --- orchestrator wiring ------------------------------------------------


@pytest.fixture
def orch(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_cleaned_dom",
                        lambda page: SimpleNamespace(html="<dom/>", synthetic_labels=[]))
    monkeypatch.setattr(orchestrator, "apply_synthetic_labels", lambda page, pairs: None)
    execute = MagicMock(name="execute_on_page", return_value={"status": "ok", "action": "type"})
    monkeypatch.setattr(orchestrator, "execute_on_page", execute)
    return SimpleNamespace(mp=monkeypatch, execute=execute)


def test_orchestrator_resume_with_intervention_skips_execute(tmp_path, orch):
    # human completed the step by hand (page changed) -> agent must NOT re-run it
    actions = iter([_risky_click(),
                    DoneAction(action="done", reason="human completed it; confirmation shown")])
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    esc = MagicMock(return_value=HandoffResult(action="resume", intervened=True,
                                               url_before=PAGE.url, url_after=f"{APP}/member/10003"))
    orch.mp.setattr(orchestrator, "request_escalation", esc)
    logger = StepLogger(tmp_path / "run")

    code, _ = run_loop("open sub-account for 10003", PAGE, logger,
                       max_steps=10, target_app=APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_OK
    esc.assert_called_once()
    orch.execute.assert_not_called()                       # the risky action never ran
    s0 = _steps(logger.run_dir)[0]
    assert s0["guardrail"]["tier"] == "confirm"
    assert s0["guardrail"]["allowed_automatically"] is False
    assert s0["handoff"]["action"] == "resume"
    assert s0["outcome"]["status"] == "resumed"
    assert "manually" in s0["outcome"]["note"]


def test_orchestrator_resume_without_intervention_executes_the_original_action(tmp_path, orch):
    # human did NOT touch the browser -> they APPROVED the action -> it executes
    risky = _risky_click()
    actions = iter([risky, DoneAction(action="done", reason="submitted; confirmation shown")])
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    esc = MagicMock(return_value=HandoffResult(action="resume", intervened=False,
                                               url_before=PAGE.url, url_after=PAGE.url))
    orch.mp.setattr(orchestrator, "request_escalation", esc)
    logger = StepLogger(tmp_path / "run")

    code, _ = run_loop("open sub-account for 10003", PAGE, logger,
                       max_steps=10, target_app=APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_OK
    esc.assert_called_once()
    orch.execute.assert_called_once()                      # the ORIGINAL action ran
    assert orch.execute.call_args.args[1] is risky
    s0 = _steps(logger.run_dir)[0]
    assert s0["handoff"]["action"] == "resume"
    assert s0["handoff"]["intervened"] is False
    assert s0["outcome"]["status"] == "ok"                 # execute_on_page result
    assert s0["outcome"]["note"] == "approved by human; executed as proposed"


def test_orchestrator_approved_action_lets_the_flow_progress_no_re_escalation(tmp_path, orch):
    # the repro: pre-fix, resume-without-intervention skipped execution, the loop
    # re-observed, re-proposed the SAME action, and escalated again forever.
    actions = iter([
        _risky_click(),                                            # step 1: escalates once
        _safe_type(value="Sub-account type", text="savings"),      # step 2: after approval
        DoneAction(action="done", reason="confirmation shown"),
    ])
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    esc = MagicMock(return_value=HandoffResult(action="resume", intervened=False,
                                               url_before=PAGE.url, url_after=PAGE.url))
    orch.mp.setattr(orchestrator, "request_escalation", esc)
    logger = StepLogger(tmp_path / "run")

    code, _ = run_loop("open sub-account for 10003", PAGE, logger,
                       max_steps=10, target_app=APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_OK
    assert esc.call_count == 1                             # escalated ONCE, not per iteration
    assert orch.execute.call_count == 2                    # the click + the following type both ran


def test_orchestrator_reject_stops_the_run_cleanly(tmp_path, orch):
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: _risky_click())
    orch.mp.setattr(orchestrator, "request_escalation",
                    lambda *a, **k: HandoffResult(action="reject"))
    logger = StepLogger(tmp_path / "run")

    code, reason = run_loop("g", PAGE, logger, max_steps=10,
                            target_app=APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_INCOMPLETE
    assert reason == "human rejected escalation at step 1"
    orch.execute.assert_not_called()
    assert _steps(logger.run_dir)[0]["handoff"]["action"] == "reject"


def test_orchestrator_safe_action_is_checked_then_auto_executed(tmp_path, orch):
    actions = iter([_safe_type(), DoneAction(action="done", reason="ok")])
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    orch.mp.setattr(orchestrator, "request_escalation",
                    lambda *a, **k: pytest.fail("a safe action must not escalate"))
    logger = StepLogger(tmp_path / "run")

    code, _ = run_loop("g", PAGE, logger, max_steps=10,
                       target_app=APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_OK
    assert orch.execute.call_count == 1
    s0 = _steps(logger.run_dir)[0]
    assert s0["guardrail"] == {
        "allowed_automatically": True, "tier": "safe",
        "allowlist_violation": False, "reason": s0["guardrail"]["reason"],
    }


def test_orchestrator_type_into_sensitive_field_is_blocked_and_redacted_in_log(tmp_path, orch):
    # a `type` into a Passphrase field -> blocked -> escalate (never executes);
    # the recorded decision's value is redacted, never the real 'vault'.
    orch.mp.setattr(orchestrator, "ask_claude",
                    lambda *a, **k: _safe_type(value="Passphrase", text="vault"))
    orch.mp.setattr(orchestrator, "request_escalation",
                    lambda *a, **k: HandoffResult(action="reject"))
    logger = StepLogger(tmp_path / "run")

    code, _ = run_loop("g", PAGE, logger, max_steps=10,
                       target_app=APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_INCOMPLETE
    orch.execute.assert_not_called()
    s0 = _steps(logger.run_dir)[0]
    assert s0["guardrail"]["tier"] == "blocked"
    assert s0["decision"]["text"] == "[REDACTED]"      # never the real 'vault' in the log
    assert "vault" not in json.dumps(s0)


def test_orchestrator_sensitive_type_flagged_requires_human_value_and_never_executes(tmp_path, orch):
    # the agent proposes a guessed SSN -> blocked -> escalation is told a human
    # must supply the value -> resume(intervened) skips it -> flow progresses.
    actions = iter([
        _safe_type(value="Verify member SSN", text="000-00-0000"),   # the fabricated guess
        DoneAction(action="done", reason="human entered SSN; confirmation shown"),
    ])
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    esc = MagicMock(return_value=HandoffResult(
        action="resume", intervened=True,
        note="human entered the sensitive value directly; agent will re-observe"))
    orch.mp.setattr(orchestrator, "request_escalation", esc)
    logger = StepLogger(tmp_path / "run")

    code, _ = run_loop("open sub-account for 10003", PAGE, logger,
                       max_steps=10, target_app=APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_OK
    esc.assert_called_once()
    orch.execute.assert_not_called()                   # the guessed SSN was NEVER typed
    ctx = esc.call_args.args[2]
    assert ctx.requires_human_value is True            # escalation knew it was sensitive
    s0 = _steps(logger.run_dir)[0]
    assert s0["guardrail"]["tier"] == "blocked"
    # the agent didn't type anything -- a human did, off to the side. The log
    # says so; it does not restate the agent's fabricated placeholder.
    assert s0["decision"]["text"] == "[not executed by agent; human entered a value directly]"
    assert "000-00-0000" not in json.dumps(s0)


def test_manual_intervention_never_writes_the_typed_value_to_json_or_console(tmp_path, orch, capsys):
    # regression for the "resumed (manual)" evidence gap: step 6 in a real run
    # logged the agent's FABRICATED placeholder for an SSN field in plain text.
    fabricated = "000-00-0000"      # what the agent proposed
    real_ssn = "912-18-2247"        # what a human might actually have typed
    actions = iter([
        _safe_type(value="Verify member SSN", text=fabricated),
        DoneAction(action="done", reason="confirmation shown"),
    ])
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    orch.mp.setattr(orchestrator, "request_escalation",
                    lambda *a, **k: HandoffResult(action="resume", intervened=True))
    logger = StepLogger(tmp_path / "run")

    run_loop("open sub-account for 10003", PAGE, logger,
             max_steps=10, target_app=APP, max_auto_tier=RiskTier.SAFE)

    step_json = json.dumps(_steps(logger.run_dir)[0])
    console = capsys.readouterr().out

    for leak in (fabricated, real_ssn):
        assert leak not in step_json, f"{leak!r} leaked into steps.jsonl"
        assert leak not in console, f"{leak!r} leaked to the console"

    s0 = _steps(logger.run_dir)[0]
    assert s0["decision"]["target"]["value"] == "Verify member SSN"   # WHAT it proposed: kept
    assert s0["decision"]["text"] == "[not executed by agent; human entered a value directly]"
    assert "human handled 'Verify member SSN' manually" in console


def test_manual_intervention_omits_value_for_a_non_sensitive_type_too(tmp_path, orch):
    # the rule is "agent didn't do it", not "it was sensitive" -- an off-app
    # (allowlist-violation) safe type that a human handles is also value-free.
    actions = iter([
        _safe_type(value="Member number", text="10003"),
        DoneAction(action="done", reason="ok"),
    ])
    orch.mp.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    orch.mp.setattr(orchestrator, "request_escalation",
                    lambda *a, **k: HandoffResult(action="resume", intervened=True))
    logger = StepLogger(tmp_path / "run")

    run_loop("g", SimpleNamespace(url="http://evil.example.com/"), logger,
             max_steps=10, target_app=APP, max_auto_tier=RiskTier.SAFE)

    s0 = _steps(logger.run_dir)[0]
    assert s0["guardrail"]["allowlist_violation"] is True
    assert s0["decision"]["text"] == "[not executed by agent; human entered a value directly]"


# --- replay wiring -----------------------------------------------------


class RPage:
    def __init__(self, url=f"{APP}/member", text="detail page, no divergence markers"):
        self.url = url
        self._text = text

    def goto(self, _url):
        pass

    def inner_text(self, _selector):
        return self._text


def _capability(*, steps=None):
    return Capability(
        capability_id="open-sub-account", version="0.1.0", name="x", description="x",
        created_from_run="r", target_app=APP,
        parameters=[Parameter(name="member_number", type="int", description="d", example="10003")],
        steps=steps if steps is not None else [
            CapabilityStep(step_number=1, action="type",
                           target=Target(strategy="label", value="Member number"),
                           value_template="{member_number}"),
            CapabilityStep(step_number=2, action="click",                       # risky
                           target=Target(strategy="text_contains", value="Process")),
        ],
        outputs=[OutputSpec(name="new_account", type="str", description="d",
                            extraction=Extraction(strategy="text_contains", target="Savings"))],
        success_condition=SuccessCondition(strategy="text_contains", target="Savings",
                                           description="d"),
    )


@pytest.fixture
def rep(monkeypatch):
    monkeypatch.setattr(engine, "get_cleaned_dom",
                        lambda page: SimpleNamespace(html="", synthetic_labels=[]))
    monkeypatch.setattr(engine, "apply_synthetic_labels", lambda p, x: None)
    execute = MagicMock(name="execute_on_page")
    monkeypatch.setattr(engine, "execute_on_page", execute)
    monkeypatch.setattr(engine, "resolve_locator",
                        lambda p, s, v: SimpleNamespace(
                            count=lambda: 1,
                            first=SimpleNamespace(inner_text=lambda: "812.55")))
    return SimpleNamespace(mp=monkeypatch, execute=execute)


def test_replay_risky_step_reject_is_a_clean_failure(rep):
    esc = MagicMock(return_value=HandoffResult(action="reject"))
    rep.mp.setattr(engine, "request_escalation", esc)

    result = replay_capability(_capability(), {"member_number": "10003"}, RPage(),
                               retry_wait=0, max_auto_tier=RiskTier.SAFE, run_id="rid")

    assert result.status == "failure"
    assert result.failure.step_number == 2
    assert "human rejected escalation at step 2" in result.failure.message
    esc.assert_called_once()
    assert rep.execute.call_count == 1                     # step 1 ran; step 2 (risky) did not


def test_replay_resume_with_intervention_skips_execute_still_reaches_success(rep):
    esc = MagicMock(return_value=HandoffResult(action="resume", intervened=True))
    rep.mp.setattr(engine, "request_escalation", esc)

    result = replay_capability(_capability(), {"member_number": "10003"}, RPage(),
                               retry_wait=0, max_auto_tier=RiskTier.SAFE, run_id="rid")

    assert result.status == "success"
    assert result.outputs == {"new_account": "812.55"}
    assert rep.execute.call_count == 1                     # step 2 skipped (human did it)


def test_replay_resume_without_intervention_executes_the_original_step(rep):
    esc = MagicMock(return_value=HandoffResult(action="resume", intervened=False))
    rep.mp.setattr(engine, "request_escalation", esc)

    result = replay_capability(_capability(), {"member_number": "10003"}, RPage(),
                               retry_wait=0, max_auto_tier=RiskTier.SAFE, run_id="rid")

    assert result.status == "success"
    assert result.outputs == {"new_account": "812.55"}
    esc.assert_called_once()
    assert rep.execute.call_count == 2                     # step 1 + step 2 (approved, executed)


def test_replay_offapp_url_escalates_before_executing(rep):
    records: list[dict] = []
    esc = MagicMock(return_value=HandoffResult(action="reject"))
    rep.mp.setattr(engine, "request_escalation", esc)
    one_step = [CapabilityStep(step_number=1, action="type",
                               target=Target(strategy="label", value="Member number"),
                               value_template="{member_number}")]

    result = replay_capability(
        _capability(steps=one_step), {"member_number": "10003"},
        RPage(url="http://evil.example.com/"),
        retry_wait=0, max_auto_tier=RiskTier.CONFIRM, run_id="rid",
        on_step=records.append,
    )

    assert result.status == "failure"
    esc.assert_called_once()
    rep.execute.assert_not_called()                        # a safe type, but off-app -> blocked
    guard = next(r["guardrail"] for r in records if "guardrail" in r)
    assert guard["allowlist_violation"] is True
