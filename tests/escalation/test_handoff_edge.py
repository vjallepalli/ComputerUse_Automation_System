"""Adversarial edge cases for escalation.handoff._prompt / request_escalation:
bad input handling, exception propagation from input_fn, and the bounded
sensitive-value loop. input() is faked; no real browser.

Conclusion of this pass: no bug here -- the behaviours below are deliberate and
defensible. Tests pin them so a future change is a conscious one.
"""

import pytest

from escalation.handoff import EscalationContext, request_escalation
from schema.guardrail import GuardrailDecision, RiskTier


class FakePage:
    def __init__(self, url="http://127.0.0.1:5001/member/10003/sub-account"):
        self.url = url

    def content(self):
        return "<html><body><table><tr><td>Confirm</td></tr></table></body></html>"

    def screenshot(self, path):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n")


def _decision(tier=RiskTier.CONFIRM):
    return GuardrailDecision(
        allowed_automatically=False, tier=tier, allowlist_violation=False,
        reason="click 'Process' contains write keyword 'process' -> confirm; escalate",
    )


def _ctx(tmp_path, requires_human_value=False):
    return EscalationContext(
        run_id="run-x", capability_id_or_goal="goal", step_number=3,
        action_summary="click 'Process'", out_root=tmp_path,
        requires_human_value=requires_human_value,
    )


class Script:
    """input_fn that returns each queued answer, then raises `end` (default EOF)."""

    def __init__(self, *answers, end=EOFError):
        self._answers = list(answers)
        self._end = end
        self.calls = 0

    def __call__(self, _prompt):
        self.calls += 1
        if self._answers:
            return self._answers.pop(0)
        raise self._end()


# --- garbage input: re-prompts, bounded by resume/reject/EOF ------------


def test_repeated_garbage_then_reject_terminates_and_counts_every_prompt(tmp_path, capsys):
    script = Script("what?", "help", "yes please", "reject")
    result = request_escalation(FakePage(), _decision(), _ctx(tmp_path), input_fn=script)

    assert result.action == "reject"
    assert script.calls == 4                       # 3 rejected + the real answer
    assert "please type 'resume' or 'reject'" in capsys.readouterr().out


def test_garbage_then_eof_degrades_to_reject(tmp_path):
    script = Script("blah", "nope", end=EOFError)
    result = request_escalation(FakePage(), _decision(), _ctx(tmp_path), input_fn=script)
    assert result.action == "reject"


def test_stopiteration_from_input_fn_is_treated_as_eof_reject(tmp_path):
    # e.g. a test/driver feeding a finite iterator of answers that runs dry.
    script = Script("garbage", end=StopIteration)
    result = request_escalation(FakePage(), _decision(), _ctx(tmp_path), input_fn=script)
    assert result.action == "reject"


def test_case_and_whitespace_are_normalised_before_matching(tmp_path):
    for answer in ("  RESUME  ", "Resume", "reSUMe\t"):
        res = request_escalation(FakePage(), _decision(), _ctx(tmp_path),
                                 input_fn=lambda _p, a=answer: a)
        assert res.action == "resume"


# --- non-EOF exceptions from input_fn propagate (by design) ------------


def test_keyboardinterrupt_from_input_fn_propagates(tmp_path):
    # Ctrl-C during a guardrail stop must abort the run, not be swallowed into
    # a silent "reject". Only EOFError / StopIteration are caught.
    def boom(_prompt):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        request_escalation(FakePage(), _decision(), _ctx(tmp_path), input_fn=boom)


def test_unexpected_exception_from_input_fn_is_not_masked(tmp_path):
    # A programming error in a custom input_fn should surface, not turn into a
    # bogus rejection that looks like a human decision.
    def boom(_prompt):
        raise RuntimeError("stdin adapter broke")

    with pytest.raises(RuntimeError, match="stdin adapter broke"):
        request_escalation(FakePage(), _decision(), _ctx(tmp_path), input_fn=boom)


# --- sensitive-value loop is bounded ---------------------------------


def test_sensitive_value_bare_resume_reprompts_then_reject_exits(tmp_path, capsys):
    # requires_human_value + page unchanged: 'resume' is refused (agent's value
    # is a fabrication), re-prompts, and 'reject' always ends it. Never silently
    # executes, never loops forever.
    script = Script("resume", "resume", "reject")
    result = request_escalation(FakePage(), _decision(RiskTier.BLOCKED),
                                _ctx(tmp_path, requires_human_value=True),
                                input_fn=script)
    assert result.action == "reject"
    assert script.calls == 3
    out = capsys.readouterr().out
    assert "No value entered yet" in out
    assert result.note == "human rejected; the required sensitive value was not provided"


def test_sensitive_value_bare_resume_then_eof_also_exits(tmp_path):
    script = Script("resume", "resume", end=EOFError)
    result = request_escalation(FakePage(), _decision(RiskTier.BLOCKED),
                                _ctx(tmp_path, requires_human_value=True),
                                input_fn=script)
    assert result.action == "reject"
