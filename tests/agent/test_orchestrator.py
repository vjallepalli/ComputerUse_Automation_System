"""Orchestrator control flow, with decide/act mocked -- no browser, no API.

End-to-end against the live Flask app + real API is deliberately NOT here (see
the note in the step-5 hand-off); these cover loop termination, exit codes, and
the step-log shape.
"""

import json
import re
from types import SimpleNamespace

import pytest

from agent import orchestrator
from agent.decide import AgentActionError
from agent.orchestrator import EXIT_INCOMPLETE, EXIT_OK, EXIT_STARTUP, StepLogger, run_loop
from schema.action import DoneAction, Target, TypeAction
from schema.guardrail import RiskTier
from surface.executor import SelectorResolutionError

TARGET_APP = "http://127.0.0.1:5001"
_PAGE = SimpleNamespace(url="http://127.0.0.1:5001/member")


@pytest.fixture
def mocked_surface(monkeypatch):
    """observe() side is stubbed: fixed cleaned DOM, label push is a no-op.
    Escalation is wired to fail loudly -- these tests only use safe actions."""
    monkeypatch.setattr(
        orchestrator, "get_cleaned_dom",
        lambda page: SimpleNamespace(html="<dom/>", synthetic_labels=[
            {"selector": 'input[name="q"]', "match_index": 0, "label": "Member number"}
        ]),
    )
    monkeypatch.setattr(orchestrator, "apply_synthetic_labels", lambda page, pairs: [])
    monkeypatch.setattr(orchestrator, "request_escalation",
                        lambda *a, **k: pytest.fail("a safe action must not escalate"))
    return monkeypatch


def _type_action(value="Member number", text="10003"):
    return TypeAction(action="type", target=Target(strategy="label", value=value), text=text)


def _done_action(reason="found the savings balance"):
    return DoneAction(action="done", reason=reason)


def _ok_result():
    return {"status": "ok", "action": "type", "strategy": "label",
            "value": "Member number", "text": "10003", "resolved_count": 1}


def _read_steps(run_dir):
    lines = (run_dir / "steps.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# --- termination + exit codes --------------------------------------------


def test_done_action_stops_loop_and_exits_zero(tmp_path, mocked_surface):
    actions = iter([_type_action(), _done_action()])
    mocked_surface.setattr(orchestrator, "ask_claude",
                           lambda *a, **k: next(actions))
    mocked_surface.setattr(orchestrator, "execute_on_page",
                           lambda page, action: _ok_result())
    logger = StepLogger(tmp_path / "run")

    code, reason = run_loop("goal", _PAGE, logger, max_steps=25, target_app=TARGET_APP)

    assert code == EXIT_OK
    assert "done at step 2" in reason
    steps = _read_steps(logger.run_dir)
    assert [s["step"] for s in steps] == [1, 2]
    assert steps[1]["decision"]["action"] == "done"
    assert steps[1]["outcome"] == {"status": "done", "reason": "found the savings balance"}


def test_max_steps_exceeded_stops_and_exits_nonzero(tmp_path, mocked_surface):
    mocked_surface.setattr(orchestrator, "ask_claude",
                           lambda *a, **k: _type_action())  # never done
    mocked_surface.setattr(orchestrator, "execute_on_page",
                           lambda page, action: _ok_result())
    logger = StepLogger(tmp_path / "run")

    code, reason = run_loop("goal", _PAGE, logger, max_steps=3, target_app=TARGET_APP)

    assert code == EXIT_INCOMPLETE
    assert "reached max steps (3)" in reason
    assert len(_read_steps(logger.run_dir)) == 3


def test_agent_action_error_stops_and_exits_nonzero(tmp_path, mocked_surface):
    def boom(*a, **k):
        raise AgentActionError("model proposed an invalid action")

    mocked_surface.setattr(orchestrator, "ask_claude", boom)
    mocked_surface.setattr(orchestrator, "execute_on_page",
                           lambda *a, **k: pytest.fail("execute should not run"))
    logger = StepLogger(tmp_path / "run")

    code, reason = run_loop("goal", _PAGE, logger, max_steps=25, target_app=TARGET_APP)

    assert code == EXIT_INCOMPLETE
    assert "AgentActionError at step 1" in reason
    steps = _read_steps(logger.run_dir)
    assert len(steps) == 1
    assert steps[0]["decision"] is None
    assert steps[0]["error"]["type"] == "AgentActionError"
    assert steps[0]["outcome"] is None


def test_selector_resolution_error_stops_and_exits_nonzero(tmp_path, mocked_surface):
    mocked_surface.setattr(orchestrator, "ask_claude", lambda *a, **k: _type_action())

    def boom(page, action):
        raise SelectorResolutionError("label='Member number' resolved to 0 elements")

    mocked_surface.setattr(orchestrator, "execute_on_page", boom)
    logger = StepLogger(tmp_path / "run")

    code, reason = run_loop("goal", _PAGE, logger, max_steps=25, target_app=TARGET_APP)

    assert code == EXIT_INCOMPLETE
    assert "SelectorResolutionError at step 1" in reason
    steps = _read_steps(logger.run_dir)
    assert len(steps) == 1
    assert steps[0]["decision"]["action"] == "type"   # decision captured even on failure
    assert steps[0]["error"]["type"] == "SelectorResolutionError"
    assert steps[0]["outcome"] is None


# --- step-log shape -----------------------------------------------------


def test_step_log_has_one_well_formed_line_per_step(tmp_path, mocked_surface):
    actions = iter([_type_action(), _done_action()])
    mocked_surface.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    mocked_surface.setattr(orchestrator, "execute_on_page",
                           lambda page, action: _ok_result())
    logger = StepLogger(tmp_path / "run")

    run_loop("goal", _PAGE, logger, max_steps=25, target_app=TARGET_APP)

    steps = _read_steps(logger.run_dir)
    assert len(steps) == 2
    for i, s in enumerate(steps, start=1):
        assert set(s) == {"step", "ts", "url", "dom", "synthetic_labels",
                          "decision", "guardrail", "handoff", "outcome", "error"}
        assert s["step"] == i
        assert set(s["dom"]) == {"sha256", "chars", "path"}
        dom_file = logger.run_dir / s["dom"]["path"]
        assert dom_file.exists()
        assert s["dom"]["chars"] == len(dom_file.read_text())
        assert s["synthetic_labels"][0]["label"] == "Member number"

    # the type step was guardrail-checked (safe -> auto); the done step was not
    assert steps[0]["guardrail"]["tier"] == "safe"
    assert steps[0]["guardrail"]["allowed_automatically"] is True
    assert steps[1]["guardrail"] is None


def test_history_accumulates_and_is_passed_forward(tmp_path, mocked_surface):
    seen_history_lengths = []

    def capture(goal, dom, history, client=None):
        seen_history_lengths.append(len(history))
        return _done_action() if len(history) == 2 else _type_action()

    mocked_surface.setattr(orchestrator, "ask_claude", capture)
    mocked_surface.setattr(orchestrator, "execute_on_page",
                           lambda page, action: _ok_result())
    logger = StepLogger(tmp_path / "run")

    code, _ = run_loop("goal", _PAGE, logger, max_steps=25, target_app=TARGET_APP)

    assert code == EXIT_OK
    assert seen_history_lengths == [0, 1, 2]  # grows by one per executed step


def test_console_prints_one_line_per_step(tmp_path, mocked_surface, capsys):
    actions = iter([_type_action(), _done_action("savings balance is 15220.00")])
    mocked_surface.setattr(orchestrator, "ask_claude", lambda *a, **k: next(actions))
    mocked_surface.setattr(orchestrator, "execute_on_page",
                           lambda page, action: _ok_result())

    run_loop("goal", _PAGE, StepLogger(tmp_path / "run"), max_steps=25, target_app=TARGET_APP)

    out = capsys.readouterr().out
    assert "[1] type '10003' into 'Member number' -> ok" in out
    assert "[2] done: savings balance is 15220.00" in out


# --- CLI startup ------------------------------------------------------


def test_main_exits_startup_when_api_key_missing(monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "_load_dotenv", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(orchestrator, "run_discovery",
                        lambda *a, **k: pytest.fail("must not reach the loop"))

    code = orchestrator.main(["--goal", "anything"])

    assert code == EXIT_STARTUP
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_main_passes_parsed_args_through_to_run_discovery(monkeypatch):
    monkeypatch.setattr(orchestrator, "_load_dotenv", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TARGET_APP_BASE_URL", "http://127.0.0.1:5001")
    captured = {}

    def fake_run(goal, **kwargs):
        captured["goal"] = goal
        captured.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(orchestrator, "run_discovery", fake_run)

    code = orchestrator.main(["--goal", "read savings balance", "--max-steps", "7"])

    assert code == EXIT_OK
    assert captured["goal"] == "read savings balance"
    assert captured["max_steps"] == 7
    assert captured["start_url"] == "http://127.0.0.1:5001/member"
    assert "artifacts/runs" in str(captured["run_dir"]).replace("\\", "/")


# --- regression: the multi-field form no longer loops on one field --------


class _StatefulFormPage:
    """content() is the stale served HTML (value="" always); evaluate() returns
    the LIVE field values, reflecting fills so far -- exactly Playwright's
    attribute-vs-property gap. Uses the REAL get_cleaned_dom (not the stub)."""

    _STALE = (
        "<table>"
        '<tr><td>Field A</td><td><input name="fa" type="text" value=""></td></tr>'
        '<tr><td>Field B</td><td><input name="fb" type="text" value=""></td></tr>'
        "</table>"
    )

    def __init__(self):
        self.url = "http://127.0.0.1:5001/member/10003/sub-account"
        self.values = {"fa": "", "fb": ""}

    def content(self):
        return self._STALE                       # never reflects fills -- the bug

    def evaluate(self, _js):
        return [{"tag": "input", "type": "text", "key": k, "value": v}
                for k, v in self.values.items()]

    def fill_field(self, name, text):
        self.values[name] = text


def test_dom_shown_when_deciding_field_b_shows_field_a_already_filled(tmp_path, monkeypatch):
    page = _StatefulFormPage()
    doms_seen: list[str] = []

    def fake_ask(goal, dom, history, client=None):
        doms_seen.append(dom)
        n = len(doms_seen)
        if n == 1:
            return TypeAction(action="type", text="Savings",
                              target=Target(strategy="label", value="Field A"))
        if n == 2:
            return TypeAction(action="type", text="500",
                              target=Target(strategy="label", value="Field B"))
        return DoneAction(action="done", reason="both fields filled")

    def fake_execute(pg, action):
        pg.fill_field({"Field A": "fa", "Field B": "fb"}[action.target.value], action.text)
        return {"status": "ok", "action": "type",
                "value": action.target.value, "text": action.text}

    monkeypatch.setattr(orchestrator, "ask_claude", fake_ask)
    monkeypatch.setattr(orchestrator, "execute_on_page", fake_execute)
    monkeypatch.setattr(orchestrator, "apply_synthetic_labels", lambda p, x: None)
    monkeypatch.setattr(orchestrator, "request_escalation",
                        lambda *a, **k: pytest.fail("no escalation expected"))
    # NB: get_cleaned_dom is NOT stubbed -- the real one runs against the fake page

    code, _ = run_loop("fill Field A then Field B", page, StepLogger(tmp_path / "run"),
                       max_steps=6, target_app=TARGET_APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_OK
    # the DOM handed to ask_claude when it decides Field B shows Field A filled --
    # so a real model would move on, not re-type Field A
    assert 'value="Savings"' in doms_seen[1]
    assert 'name="fa"' in doms_seen[1]
    # exactly 3 asks (A, B, done) -- no re-targeting churn
    assert len(doms_seen) == 3


class _StatefulSubAccountPage:
    """Two non-sensitive fields + one 'Verify member SSN' field. content() stale;
    evaluate() live. Real get_cleaned_dom runs against it."""

    _STALE = (
        "<table>"
        '<tr><td>Sub-account type</td><td><input name="f1" type="text" value=""></td></tr>'
        '<tr><td>Initial deposit</td><td><input name="f2" type="text" value=""></td></tr>'
        '<tr><td>Verify member SSN</td><td><input name="f3" type="text" value=""></td>'
        "<td>NNN-NN-NNNN</td></tr>"
        "</table>"
    )

    def __init__(self):
        self.url = "http://127.0.0.1:5001/member/10003/sub-account"
        self.values = {"f1": "", "f2": "", "f3": ""}

    def content(self):
        return self._STALE

    def evaluate(self, _js):
        return [{"tag": "input", "type": "text", "key": k, "value": v}
                for k, v in self.values.items()]


def test_empty_sensitive_field_is_shown_empty_to_the_model_not_masked(tmp_path, monkeypatch):
    # regression: with f1/f2 filled and f3 (SSN) untouched, the DOM the model
    # decides the next action from MUST show f3 empty -- if it shows [MASKED] the
    # model thinks it's done and clicks Process, skipping the human SSN hand-off.
    page = _StatefulSubAccountPage()
    doms_seen: list[str] = []

    def fake_ask(goal, dom, history, client=None):
        doms_seen.append(dom)
        n = len(doms_seen)
        if n == 1:
            return TypeAction(action="type", text="Savings",
                              target=Target(strategy="label", value="Sub-account type"))
        if n == 2:
            return TypeAction(action="type", text="500.00",
                              target=Target(strategy="label", value="Initial deposit"))
        return DoneAction(action="done", reason="captured the decision-time DOM")

    def fake_execute(pg, action):
        pg.values[{"Sub-account type": "f1", "Initial deposit": "f2"}[action.target.value]] = action.text
        return {"status": "ok", "action": "type",
                "value": action.target.value, "text": action.text}

    monkeypatch.setattr(orchestrator, "ask_claude", fake_ask)
    monkeypatch.setattr(orchestrator, "execute_on_page", fake_execute)
    monkeypatch.setattr(orchestrator, "apply_synthetic_labels", lambda p, x: None)
    monkeypatch.setattr(orchestrator, "request_escalation",
                        lambda *a, **k: pytest.fail("no escalation on these two safe types"))

    code, _ = run_loop("open a sub-account for 10003", page, StepLogger(tmp_path / "run"),
                       max_steps=6, target_app=TARGET_APP, max_auto_tier=RiskTier.SAFE)

    assert code == EXIT_OK
    deciding_dom = doms_seen[2]                         # DOM for the 3rd decision
    f3 = re.search(r'<input[^>]*name="f3"[^>]*/?>', deciding_dom).group(0)
    assert 'value=""' in f3                             # empty, NOT [MASKED]
    assert "[MASKED]" not in f3
    assert 'value="Savings"' in deciding_dom            # the two filled ones still shown
    assert 'value="500.00"' in deciding_dom
