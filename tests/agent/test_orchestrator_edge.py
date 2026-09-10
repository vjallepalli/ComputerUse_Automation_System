"""Adversarial edge cases for the discovery loop's termination.

Scenario B3 of the adversarial pass ("impossible goal / --max-steps 1 against a
multi-step flow -> clean 'reached max steps' exit, not a hang or crash") cannot
be run live here -- discovery needs the Anthropic API, and this session has no
key / must not call it. These mock `ask_claude` to a model that is never
"done", which exercises the same termination code the live run would hit.

`test_orchestrator.py::test_max_steps_exceeded_stops_and_exits_nonzero` covers
max_steps=3; this adds the max_steps=1 boundary and checks the meta.json a
recorder would later read.
"""

import json
from types import SimpleNamespace

import pytest

from agent import orchestrator
from agent.orchestrator import EXIT_INCOMPLETE, StepLogger, run_loop
from schema.action import Target, TypeAction

TARGET_APP = "http://127.0.0.1:5001"
_PAGE = SimpleNamespace(url="http://127.0.0.1:5001/member")


@pytest.fixture
def mocked_surface(monkeypatch):
    monkeypatch.setattr(
        orchestrator, "get_cleaned_dom",
        lambda page: SimpleNamespace(html="<dom/>", synthetic_labels=[]),
    )
    monkeypatch.setattr(orchestrator, "apply_synthetic_labels", lambda page, pairs: [])
    monkeypatch.setattr(orchestrator, "request_escalation",
                        lambda *a, **k: pytest.fail("a safe action must not escalate"))
    monkeypatch.setattr(orchestrator, "ask_claude",
                        lambda *a, **k: TypeAction(
                            action="type",
                            target=Target(strategy="label", value="Member number"),
                            text="10003"))
    monkeypatch.setattr(orchestrator, "execute_on_page",
                        lambda page, action: {"status": "ok", "action": "type",
                                              "strategy": "label", "value": "Member number",
                                              "text": "10003", "resolved_count": 1})
    return monkeypatch


def test_max_steps_one_against_a_multi_step_flow_exits_cleanly(tmp_path, mocked_surface):
    logger = StepLogger(tmp_path / "run")
    code, reason = run_loop("open a sub-account and reach confirmation", _PAGE, logger,
                            max_steps=1, target_app=TARGET_APP)

    assert code == EXIT_INCOMPLETE
    assert "reached max steps (1)" in reason
    steps = [json.loads(l) for l in (logger.run_dir / "steps.jsonl").read_text().splitlines()]
    assert len(steps) == 1                       # ran exactly one iteration, then stopped


def test_impossible_goal_runs_the_full_budget_then_stops(tmp_path, mocked_surface):
    logger = StepLogger(tmp_path / "run")
    code, reason = run_loop("do something the UI cannot do", _PAGE, logger,
                            max_steps=5, target_app=TARGET_APP)

    assert code == EXIT_INCOMPLETE
    assert "reached max steps (5)" in reason
    steps = [json.loads(l) for l in (logger.run_dir / "steps.jsonl").read_text().splitlines()]
    assert len(steps) == 5


def test_a_maxed_out_run_transcript_is_refused_by_the_recorder(tmp_path):
    # B3 downstream guarantee: a "reached max steps" run (exit_code != 0, no
    # 'done') must never be distilled into a capability.
    from agent.record import RecorderError, record_capability

    run = tmp_path / "run"
    run.mkdir()
    (run / "meta.json").write_text(json.dumps({
        "run_id": "run", "goal": "impossible", "start_url": TARGET_APP,
        "exit_code": EXIT_INCOMPLETE,
        "stop_reason": "reached max steps (25) without a done action",
    }))
    (run / "steps.jsonl").write_text(json.dumps({
        "step": 1, "decision": {"action": "type",
                                "target": {"strategy": "label", "value": "Member number"},
                                "text": "10003"}}) + "\n")

    with pytest.raises(RecorderError, match="did not complete successfully"):
        record_capability(run, output_name="x", output_strategy="next_cell",
                          output_target="Savings", capability_id="x", name="x",
                          description="x")
