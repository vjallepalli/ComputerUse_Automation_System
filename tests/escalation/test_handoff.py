"""escalation.handoff.request_escalation: file written, DOM snapshot ref, and
the resume / reject control paths -- input() mocked, no real browser."""

from escalation.handoff import EscalationContext, request_escalation
from schema.escalation import EscalationRequest
from schema.guardrail import GuardrailDecision, RiskTier


class FakePage:
    def __init__(self, url="http://127.0.0.1:5001/member/10003/sub-account"):
        self.url = url

    def content(self):
        return "<html><body><table><tr><td>Confirm sub-account</td></tr></table></body></html>"

    def screenshot(self, path):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n")  # not a real image; just proves the path is used


def _decision():
    return GuardrailDecision(
        allowed_automatically=False, tier=RiskTier.CONFIRM, allowlist_violation=False,
        reason="click 'Process' contains write keyword 'process' -> confirm; escalate",
    )


def _context(tmp_path):
    return EscalationContext(
        run_id="run-x", capability_id_or_goal="open sub-account for member 10003",
        step_number=3, action_summary="click 'Process'", out_root=tmp_path,
    )


def _only_request_file(tmp_path):
    files = list((tmp_path / "run-x").glob("*.json"))
    assert len(files) == 1
    return files[0]


def test_resume_writes_the_request_and_returns_resume(tmp_path, capsys):
    page = FakePage()
    result = request_escalation(page, _decision(), _context(tmp_path),
                                input_fn=lambda _prompt: "resume")

    assert result.action == "resume"

    req_file = _only_request_file(tmp_path)
    req = EscalationRequest.model_validate_json(req_file.read_text())
    assert req.run_id == "run-x"
    assert req.step_number == 3
    assert req.tier is RiskTier.CONFIRM
    assert "process" in req.reason
    assert req.current_url == page.url

    # DOM snapshot is referenced by path (a bare filename), not inlined
    assert "/" not in req.dom_snapshot_path
    assert "<html" not in req_file.read_text()          # markup is not in the JSON
    dom_file = req_file.parent / req.dom_snapshot_path
    assert dom_file.is_file() and dom_file.read_text().strip() != ""
    # screenshot path also written
    assert (req_file.parent / req.screenshot_path).is_file()

    out = capsys.readouterr().out
    assert "GUARDRAIL STOP" in out and "click 'Process'" in out


def test_reject_returns_reject(tmp_path):
    result = request_escalation(FakePage(), _decision(), _context(tmp_path),
                                input_fn=lambda _prompt: "reject")
    assert result.action == "reject"
    assert _only_request_file(tmp_path).is_file()


def test_unrecognised_input_loops_until_resume_or_reject(tmp_path):
    answers = iter(["what?", "", "resume"])
    result = request_escalation(FakePage(), _decision(), _context(tmp_path),
                                input_fn=lambda _prompt: next(answers))
    assert result.action == "resume"


def test_intervened_is_true_when_the_human_changed_the_page(tmp_path):
    page = FakePage()

    def act_then_resume(_prompt):
        page.url = "http://127.0.0.1:5001/member/10003"   # human navigated
        return "resume"

    result = request_escalation(page, _decision(), _context(tmp_path),
                                input_fn=act_then_resume)
    assert result.action == "resume"
    assert result.intervened is True
    assert result.url_before != result.url_after
    assert result.dom_snapshot_after_path is not None
    assert (tmp_path / "run-x" / result.dom_snapshot_after_path).is_file()


def test_eof_on_input_is_treated_as_reject(tmp_path):
    def raise_eof(_prompt):
        raise EOFError

    result = request_escalation(FakePage(), _decision(), _context(tmp_path),
                                input_fn=raise_eof)
    assert result.action == "reject"


# --- sensitive-value escalation: 'just approve it' is not an option ----------


class _FormPage:
    """The human 'types' by mutating field_values. The cleaned DOM masks the SSN
    field, so a bare content()/url comparison can't see the human's input --
    request_escalation must fall back to the raw field values."""

    def __init__(self, url="http://127.0.0.1:5001/member/10003/sub-account"):
        self.url = url
        self.field_values = {"f3": ""}

    def content(self):
        return ('<html><body><table><tr><td>Verify member SSN</td>'
                '<td><input name="f3" value=""></td></tr></table></body></html>')

    def evaluate(self, _script):
        return [{"tag": "input", "type": "text", "key": k, "value": v}
                for k, v in self.field_values.items()]

    def screenshot(self, path):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG")


def _sensitive_decision():
    return GuardrailDecision(
        allowed_automatically=False, tier=RiskTier.BLOCKED, allowlist_violation=False,
        reason=("type into a sensitive field ('ssn' in 'verify member ssn') -> blocked; "
                "the agent must never autofill secret data"),
    )


def _sensitive_context(tmp_path):
    return EscalationContext(
        run_id="run-x", capability_id_or_goal="open sub-account for member 10003",
        step_number=5, action_summary="type into 'Verify member SSN'",
        requires_human_value=True, out_root=tmp_path,
    )


def test_sensitive_resume_without_a_real_value_re_prompts_and_does_not_proceed(tmp_path, capsys):
    answers = iter(["resume", "resume", "reject"])   # two bare approvals, then give up
    result = request_escalation(_FormPage(), _sensitive_decision(),
                                _sensitive_context(tmp_path),
                                input_fn=lambda _p: next(answers))

    assert result.action == "reject"                 # bounded -- 'reject' always exits
    assert result.intervened is False
    out = capsys.readouterr().out
    assert out.count("No value entered yet") == 2    # re-prompted on each bare 'resume'
    # instructions state WHY approving isn't allowed here
    assert "fabricated value" in out
    assert "no 'just approve it'" in out


def test_sensitive_resume_loop_terminates_on_exhausted_input(tmp_path):
    answers = iter(["resume", "resume"])             # then StopIteration
    result = request_escalation(_FormPage(), _sensitive_decision(),
                                _sensitive_context(tmp_path),
                                input_fn=lambda _p: next(answers))
    assert result.action == "reject"                 # not a crash, not an infinite loop


def test_sensitive_resume_after_human_enters_the_value_proceeds(tmp_path):
    page = _FormPage()

    def enter_value_then_resume(_prompt):
        page.field_values["f3"] = "912-33-1092"      # human typed the real SSN
        return "resume"

    result = request_escalation(page, _sensitive_decision(),
                                _sensitive_context(tmp_path),
                                input_fn=enter_value_then_resume)

    assert result.action == "resume"
    assert result.intervened is True
    assert "entered the sensitive value" in result.note
