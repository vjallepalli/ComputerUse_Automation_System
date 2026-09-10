"""replay_capability control flow, with execute_on_page / page / locators faked.

No real browser. Covers the taxonomy: success, params mismatch (step 0),
business_outcome short-circuit, one-retry recovery, hard failure after retry,
and success_condition-not-met.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from replay import engine
from replay.engine import replay_capability
from schema.action import Target
from schema.capability import (
    Capability,
    CapabilityStep,
    Extraction,
    OutputSpec,
    Parameter,
    SuccessCondition,
)
from surface.executor import SelectorResolutionError


# --- fakes ---------------------------------------------------------------


class FakePage:
    def __init__(self, texts="Member Lookup", url="http://127.0.0.1:5001/member"):
        self._texts = list(texts) if isinstance(texts, (list, tuple)) else [texts]
        self._i = 0
        self.url = url  # inside the capability's target_app -> guardrail allowlist ok
        self.goto_calls: list[str] = []

    def goto(self, url):
        self.goto_calls.append(url)

    def inner_text(self, _selector):
        return self._texts[min(self._i, len(self._texts) - 1)]

    def advance(self):
        self._i += 1


class FakeLocator:
    def __init__(self, count=1, text="", value=None):
        self._count = count
        self._text = text
        self._value = value

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def inner_text(self):
        return self._text

    def input_value(self):
        if self._value is None:
            raise RuntimeError("element has no value")
        return self._value


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(engine, "get_cleaned_dom",
                        lambda page: SimpleNamespace(html="", synthetic_labels=[]))
    monkeypatch.setattr(engine, "apply_synthetic_labels", lambda page, pairs: None)

    execute = MagicMock(name="execute_on_page")
    monkeypatch.setattr(engine, "execute_on_page", execute)

    box = {"locator": FakeLocator(count=1, text="")}
    monkeypatch.setattr(engine, "resolve_locator",
                        lambda page, strategy, value: box["locator"])

    # every step in these tests is safe + same-origin -> the guardrail must not
    # escalate; blow up loudly (and never block on input()) if it tries.
    monkeypatch.setattr(engine, "request_escalation",
                        lambda *a, **k: pytest.fail("a safe same-origin step must not escalate"))

    return SimpleNamespace(execute=execute, set_locator=box.__setitem__)


# --- capability builder ------------------------------------------------


def _capability(*, steps=None, success=("text_contains", "Savings"),
                parameters=None, outputs=None):
    return Capability(
        capability_id="lookup-savings-balance",
        version="0.1.0",
        name="Look up savings balance",
        description="d",
        created_from_run="run_lookup_10003",
        target_app="http://127.0.0.1:5001",
        parameters=parameters if parameters is not None else [
            Parameter(name="member_number", type="int", description="d", example="10003"),
        ],
        steps=steps if steps is not None else [
            CapabilityStep(step_number=1, action="type",
                           target=Target(strategy="label", value="Member number"),
                           value_template="{member_number}"),
            CapabilityStep(step_number=2, action="click",
                           target=Target(strategy="text_contains", value="Retrieve")),
        ],
        outputs=outputs if outputs is not None else [
            OutputSpec(name="savings_balance", type="str", description="d",
                       extraction=Extraction(strategy="text_contains", target="Savings")),
        ],
        success_condition=SuccessCondition(strategy=success[0], target=success[1],
                                           description="d"),
    )


_TYPE_ONLY = [CapabilityStep(
    step_number=1, action="type",
    target=Target(strategy="label", value="Member number"),
    value_template="{member_number}",
)]


# --- success --------------------------------------------------------


def test_happy_path_substitutes_param_and_returns_success(patched):
    cap = _capability()
    page = FakePage("Account detail  SAV-10003-01 Savings 812.55")
    patched.set_locator("locator", FakeLocator(count=1, text="SAV-10003-01 Savings 812.55"))

    result = replay_capability(cap, {"member_number": "10003"}, page, retry_wait=0)

    assert result.status == "success"
    assert result.outputs == {"savings_balance": "SAV-10003-01 Savings 812.55"}
    assert result.params == {"member_number": "10003"}
    assert page.goto_calls == ["http://127.0.0.1:5001"]

    typed_action = patched.execute.call_args_list[0].args[1]
    assert typed_action.action == "type"
    assert typed_action.text == "10003"                    # {member_number} substituted
    assert patched.execute.call_args_list[1].args[1].action == "click"


# --- params mismatch -> step 0, no page interaction ------------------


def test_unknown_param_name_fails_at_step_zero_before_navigating(patched):
    page = FakePage()
    result = replay_capability(_capability(), {"wrong_name": "10003"}, page, retry_wait=0)

    assert result.status == "failure"
    assert result.failure.step_number == 0
    assert page.goto_calls == []
    patched.execute.assert_not_called()


def test_wrong_param_type_fails_at_step_zero(patched):
    page = FakePage()
    result = replay_capability(
        _capability(), {"member_number": "not-a-number"}, page, retry_wait=0
    )
    assert result.status == "failure"
    assert result.failure.step_number == 0
    assert "not a valid int" in result.failure.message
    assert page.goto_calls == []


# --- business outcome short-circuits extraction --------------------


def test_business_outcome_after_a_step_skips_output_extraction(patched):
    page = FakePage(["Member Lookup", "Member Lookup",
                     "Access Restricted -- member 10004 is flagged RESTRICTED"])
    patched.execute.side_effect = lambda pg, action: pg.advance()
    patched.set_locator("locator", FakeLocator(count=0))  # extraction would fail if reached

    result = replay_capability(_capability(), {"member_number": "10004"}, page, retry_wait=0)

    assert result.status == "business_outcome"
    assert result.business_outcome.code == "restricted"
    assert result.outputs is None


# --- one retry recovers -------------------------------------------


def test_transient_selector_error_retries_once_and_succeeds(patched):
    cap = _capability(steps=_TYPE_ONLY)
    page = FakePage("detail page, no divergence markers")
    patched.execute.side_effect = [SelectorResolutionError("resolved to 0"), None]
    patched.set_locator("locator", FakeLocator(count=1, text="Savings 812.55"))

    result = replay_capability(cap, {"member_number": "10003"}, page, retry_wait=0)

    assert result.status == "success"
    assert patched.execute.call_count == 2                 # first attempt + the retry


# --- both attempts fail -> failure, no exception escapes ----------


def test_persistent_selector_error_is_a_failure_not_a_crash(patched):
    cap = _capability(steps=_TYPE_ONLY)
    page = FakePage("Member Lookup")
    patched.execute.side_effect = SelectorResolutionError(
        "label='Member number' resolved to 0 elements"
    )

    result = replay_capability(cap, {"member_number": "10003"}, page, retry_wait=0)

    assert result.status == "failure"
    assert result.failure.step_number == 1
    assert patched.execute.call_count == 2
    assert "Member number" in result.failure.expected


# --- flow finished, wrong end state -----------------------------


def test_success_condition_not_resolving_is_a_failure(patched):
    page = FakePage("a page that is not the expected end state")
    patched.set_locator("locator", FakeLocator(count=0))   # success_condition won't resolve

    result = replay_capability(_capability(), {"member_number": "10003"}, page, retry_wait=0)

    assert result.status == "failure"
    assert result.failure.step_number == 2                 # == len(steps)
    assert "end state" in result.failure.message
