"""Adversarial edge cases for replay.engine -- hostile params, wrong-shape
params, template-substitution safety, and the coercion fall-through.

Same faking approach as test_replay.py (execute_on_page / page / locators
mocked, no browser). `test_params_as_a_list_*` is a regression for a genuine
"confusing downstream error" fixed in this pass.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from replay import engine
from replay.engine import default_run_id, replay_capability
from schema.action import Target
from schema.capability import (
    Capability, CapabilityStep, Extraction, OutputSpec, Parameter, SuccessCondition,
)


class FakePage:
    def __init__(self, text="detail page", url="http://127.0.0.1:5001/member"):
        self._text = text
        self.url = url
        self.goto_calls: list[str] = []

    def goto(self, url):
        self.goto_calls.append(url)

    def inner_text(self, _sel):
        return self._text


class FakeLocator:
    def __init__(self, count=1, text=""):
        self._count, self._text = count, text

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def inner_text(self):
        return self._text

    def input_value(self):
        raise RuntimeError("no value")


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(engine, "get_cleaned_dom",
                        lambda page: SimpleNamespace(html="", synthetic_labels=[]))
    monkeypatch.setattr(engine, "apply_synthetic_labels", lambda page, pairs: None)
    execute = MagicMock(name="execute_on_page")
    monkeypatch.setattr(engine, "execute_on_page", execute)
    box = {"loc": FakeLocator(count=1, text="812.55")}
    monkeypatch.setattr(engine, "resolve_locator", lambda p, s, v: box["loc"])
    monkeypatch.setattr(engine, "request_escalation",
                        lambda *a, **k: pytest.fail("no escalation expected here"))
    return SimpleNamespace(execute=execute, set_loc=lambda l: box.__setitem__("loc", l))


def _cap(*, out_type="str", template="{member_number}", param_type="int"):
    return Capability(
        capability_id="lookup-savings-balance", version="0.1.0",
        name="n", description="d", created_from_run="r",
        target_app="http://127.0.0.1:5001",
        parameters=[Parameter(name="member_number", type=param_type, description="d",
                              example="10003")],
        steps=[CapabilityStep(step_number=1, action="type",
                              target=Target(strategy="label", value="Member number"),
                              value_template=template)],
        outputs=[OutputSpec(name="savings_balance", type=out_type, description="d",
                            extraction=Extraction(strategy="next_cell", target="Savings"))],
        success_condition=SuccessCondition(strategy="next_cell", target="Savings",
                                           description="d"),
    )


# --- wrong-shape params ------------------------------------------------


def test_params_as_a_list_is_a_clean_step_zero_failure_not_a_crash(patched):
    page = FakePage()
    result = replay_capability(_cap(), ["10003"], page, retry_wait=0)
    assert result.status == "failure"
    assert result.failure.step_number == 0
    assert "must be a dict" in result.failure.message
    assert page.goto_calls == []
    patched.execute.assert_not_called()


def test_params_as_a_string_is_a_clean_step_zero_failure(patched):
    result = replay_capability(_cap(), "10003", FakePage(), retry_wait=0)
    assert result.status == "failure"
    assert result.failure.step_number == 0


def test_extra_undeclared_param_is_rejected_before_any_page_touch(patched):
    # JUDGMENT CALL: `_validate_params` demands the param set EQUAL the declared
    # set -- an extra key is a step-0 failure, not a warn-and-ignore. Rationale:
    # an unexpected param usually means the caller has the wrong capability or a
    # typo'd name; failing loud is safer than silently dropping it.
    page = FakePage()
    result = replay_capability(
        _cap(), {"member_number": "10003", "branch": "07"}, page, retry_wait=0)
    assert result.status == "failure"
    assert result.failure.step_number == 0
    assert "unexpected" in result.failure.observed
    assert page.goto_calls == []


# --- param values with regex/format metacharacters -------------------


@pytest.mark.parametrize("value", [
    'a{b}c', '"; DROP TABLE', r'back\1slash', '{member_number}', '100%', '{{x}}',
    "line1\nline2",
])
def test_param_value_metacharacters_are_typed_verbatim_not_re_substituted(patched, value):
    # `_build_action` substitutes {name} in the TEMPLATE with str(param); the
    # substituted value is never itself scanned for {..} or regex backrefs, and
    # the locator value is the fixed `step.target.value`, not the param. So a
    # hostile value can only ever become literal typed text.
    page = FakePage("812.55")
    result = replay_capability(
        _cap(param_type="str"), {"member_number": value}, page, retry_wait=0)
    assert result.status == "success"
    typed = patched.execute.call_args_list[0].args[1]
    assert typed.text == value                        # verbatim, no re-expansion


def test_a_valid_value_substitutes_and_is_typed_exactly(patched):
    page = FakePage("812.55")
    replay_capability(_cap(), {"member_number": "10003"}, page, retry_wait=0)
    typed = patched.execute.call_args_list[0].args[1]
    assert typed.action == "type" and typed.text == "10003"


# --- coercion fall-through (JUDGMENT CALL: documented, not "fixed") ---


def test_coerce_returns_raw_text_when_it_does_not_match_the_declared_type(patched):
    # JUDGMENT CALL: `_coerce` tries int()/float()/bool and, on ValueError,
    # returns the raw string. So a float-typed output that reads "n/a" comes
    # back as the string "n/a", not an error. This is deliberate: the output
    # type is a review-time hint ("defaults to str -- refine on review"), and a
    # best-effort read that surfaces the real page text beats failing the whole
    # replay over a type annotation. Pinned here so the behaviour is a choice,
    # not an accident.
    page = FakePage("n/a")
    patched.set_loc(FakeLocator(count=1, text="n/a"))
    result = replay_capability(_cap(out_type="float"), {"member_number": "10003"},
                               page, retry_wait=0)
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "n/a"}


def test_coerce_parses_a_clean_numeric_string_to_the_declared_type(patched):
    page = FakePage("812.55")
    patched.set_loc(FakeLocator(count=1, text="812.55"))
    result = replay_capability(_cap(out_type="float"), {"member_number": "10003"},
                               page, retry_wait=0)
    assert result.outputs == {"savings_balance": 812.55}


# --- run-id uniqueness (scenario 7: back-to-back replays) ------------


def test_default_run_id_is_unique_across_a_tight_burst():
    # The old default was a bare %S UTC stamp: two replays started in the same
    # second shared a run dir and silently overwrote result.json / steps.jsonl.
    ids = {default_run_id() for _ in range(2000)}
    assert len(ids) == 2000


def test_default_run_id_prefix_is_applied():
    assert default_run_id("replay-").startswith("replay-")


# --- allowlist re-check runs through the REAL shared guardrail -----------
# Integration-flavoured: guardrail.evaluate is NOT mocked here. It proves the
# engine consults the real shared module every step and, on a violation,
# escalates instead of touching execute_on_page. (Live B1 finding: against the
# real target app this branch is unreachable because the engine always
# navigates the page to capability.target_app first and the app has no
# cross-origin links -- so a mis-pointed target_app either stays self-consistent
# or fails earlier as a clean CLI setup error. This test drives the branch by
# simulating a step that drifted off-origin.)


def test_offorigin_page_triggers_a_real_allowlist_violation_and_no_execute(monkeypatch):
    from schema.escalation import HandoffResult

    monkeypatch.setattr(engine, "get_cleaned_dom",
                        lambda page: SimpleNamespace(html="", synthetic_labels=[]))
    monkeypatch.setattr(engine, "apply_synthetic_labels", lambda page, pairs: None)
    execute = MagicMock(name="execute_on_page")
    monkeypatch.setattr(engine, "execute_on_page", execute)
    monkeypatch.setattr(engine, "resolve_locator", lambda p, s, v: FakeLocator(count=0))

    seen = {}

    def fake_escalation(page, decision, context):
        seen["decision"] = decision
        return HandoffResult(action="reject", intervened=False,
                             url_before=page.url, url_after=page.url, note="test")

    monkeypatch.setattr(engine, "request_escalation", fake_escalation)

    class OffOriginPage(FakePage):
        def goto(self, url):
            self.goto_calls.append(url)   # engine navigates here, but .url stays off-origin

    page = OffOriginPage(url="http://evil.example/phish")

    result = replay_capability(_cap(param_type="str"), {"member_number": "10003"},
                               page, retry_wait=0)

    assert result.status == "failure"
    assert seen["decision"].allowlist_violation is True          # from the REAL guardrail
    assert seen["decision"].allowed_automatically is False
    execute.assert_not_called()                                   # never reached
    assert "rejected escalation" in result.failure.message

