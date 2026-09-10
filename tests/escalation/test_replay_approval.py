"""escalation.handoff.request_replay_approval -- the pre-replay gate for a DRAFT
capability, reusing the guardrail pause/resume mechanism with a documented
"there is no page yet" adaptation of the EscalationRequest shape.

input() is faked; no browser.
"""

from types import SimpleNamespace

from escalation.handoff import request_replay_approval
from schema.escalation import EscalationRequest
from schema.guardrail import RiskTier

_CAP = SimpleNamespace(capability_id="fresh-capability", version="0.1.0",
                       name="Do a fresh thing")


def _only_request(root):
    files = list((root / "run-x").glob("*.json"))
    assert len(files) == 1
    return EscalationRequest.model_validate_json(files[0].read_text())


def test_persists_a_draft_specific_request_with_the_adapted_shape(tmp_path, capsys):
    result = request_replay_approval(_CAP, run_id="run-x", out_root=tmp_path,
                                     input_fn=lambda _p: "reject")
    assert result.action == "reject"

    req = _only_request(tmp_path)
    # adapted shape (documented): no page yet
    assert req.step_number == 0
    assert req.tier is RiskTier.BLOCKED
    assert req.current_url is None
    assert req.dom_snapshot_path is None
    assert req.screenshot_path is None
    assert req.capability_id_or_goal == "fresh-capability"
    # distinct, capability-level reason -- NOT a guardrail keyword explanation
    assert "draft" in req.reason
    assert "never been approved for unattended replay" in req.reason
    assert "process" not in req.reason and "keyword" not in req.reason

    printed = capsys.readouterr().out
    assert "REPLAY APPROVAL GATE" in printed
    assert "python -m agent.approve" in printed


def test_resume_returns_a_resume_handoff_that_does_not_promote(tmp_path):
    result = request_replay_approval(_CAP, run_id="run-x", out_root=tmp_path,
                                     input_fn=lambda _p: "resume")
    assert result.action == "resume"
    assert result.intervened is False
    assert "left as draft" in result.note


def test_reject_returns_a_reject_handoff(tmp_path):
    result = request_replay_approval(_CAP, run_id="run-x", out_root=tmp_path,
                                     input_fn=lambda _p: "reject")
    assert result.action == "reject"
    assert "not cleared for unattended replay" in result.note


def test_eof_on_input_degrades_to_reject(tmp_path):
    def eof(_p):
        raise EOFError

    result = request_replay_approval(_CAP, run_id="run-x", out_root=tmp_path, input_fn=eof)
    assert result.action == "reject"


def test_garbage_input_reprompts_then_takes_the_real_answer(tmp_path, capsys):
    answers = iter(["what?", "help", "resume"])
    result = request_replay_approval(_CAP, run_id="run-x", out_root=tmp_path,
                                     input_fn=lambda _p: next(answers))
    assert result.action == "resume"
    assert "please type 'resume' or 'reject'" in capsys.readouterr().out
