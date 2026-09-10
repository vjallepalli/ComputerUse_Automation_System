"""Adversarial edge cases for the Capability artifact schema.

Focus: things a hand-edited artifact could get wrong that must be caught at the
model boundary rather than blowing up (or silently misbehaving) inside replay.
Written during the self-directed adversarial pass. `test_duplicate_*` and
`test_empty_steps_*` are regressions for genuine gaps found in that pass.
"""

import pytest
from pydantic import ValidationError

from schema.action import Target
from schema.capability import (
    Capability,
    CapabilityStep,
    Extraction,
    OutputSpec,
    Parameter,
    SuccessCondition,
)


def _param(name="member_number", type_="int"):
    return Parameter(name=name, type=type_, description="d", example="10003")


def _type_step(n=1, value="Member number", template="{member_number}"):
    return CapabilityStep(step_number=n, action="type",
                          target=Target(strategy="label", value=value),
                          value_template=template)


def _click_step(n=2, value="Retrieve"):
    return CapabilityStep(step_number=n, action="click",
                          target=Target(strategy="text_contains", value=value))


def _output(name="savings_balance"):
    return OutputSpec(name=name, type="str", description="d",
                      extraction=Extraction(strategy="next_cell", target="Savings"))


def _capability(**overrides):
    kw = dict(
        capability_id="lookup-savings-balance",
        version="0.1.0",
        name="Look up savings balance",
        description="d",
        created_from_run="run-1",
        target_app="http://127.0.0.1:5001",
        parameters=[_param()],
        steps=[_type_step(), _click_step()],
        outputs=[_output()],
        success_condition=SuccessCondition(strategy="next_cell", target="Savings",
                                           description="d"),
    )
    kw.update(overrides)
    return Capability(**kw)


# --- the recorded happy artifact still validates -------------------------


def test_baseline_capability_is_valid():
    cap = _capability()
    assert cap.schema_version == "1.0"
    assert [s.step_number for s in cap.steps] == [1, 2]


# --- BUG: duplicate parameter / output names silently collapsed ----------
# `parameters` / `outputs` are lists but every consumer keys them by name
# (replay's `_validate_params` -> `{p.name: p.type}`, `_extract_all` ->
# `outputs[spec.name]`). Two entries sharing a name is an ambiguity that used
# to pass validation and then quietly let "last one wins" decide.


def test_duplicate_parameter_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate parameter name"):
        _capability(parameters=[
            _param(name="member_number", type_="int"),
            _param(name="member_number", type_="str"),
        ])


def test_duplicate_output_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate output name"):
        _capability(outputs=[_output(name="balance"), _output(name="balance")])


def test_distinct_parameter_names_are_fine():
    cap = _capability(
        parameters=[_param(name="member_number"), _param(name="branch_code", type_="str")],
        steps=[_type_step(template="{member_number}"),
               _type_step(n=2, value="Branch", template="{branch_code}")],
    )
    assert {p.name for p in cap.parameters} == {"member_number", "branch_code"}


# --- BUG / JUDGMENT CALL: empty steps list -----------------------------


def test_empty_steps_list_is_rejected():
    # a zero-step capability would skip straight to the success_condition check
    # and "pass" meaninglessly. The recorder already refuses this; the schema
    # now does too.
    with pytest.raises(ValidationError):
        _capability(steps=[])


# --- undeclared {param} reference ------------------------------------


def test_step_referencing_an_undeclared_param_is_rejected():
    with pytest.raises(ValidationError, match="undeclared parameter"):
        _capability(
            parameters=[_param(name="member_number")],
            steps=[_type_step(template="{account_number}")],
        )


def test_a_literal_value_template_is_not_treated_as_a_param_ref():
    cap = _capability(
        parameters=[],
        steps=[_type_step(template="USD"), _click_step()],
    )
    assert cap.steps[0].value_template == "USD"


# --- version / id string shape --------------------------------------


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0-rc1", "1.0.0.0", "01.0.0 ", ""])
def test_version_must_be_bare_semver(bad):
    with pytest.raises(ValidationError):
        _capability(version=bad)


@pytest.mark.parametrize("bad", ["Lookup", "lookup_savings", "lookup savings",
                                 "-lookup", "lookup-", "lookup--savings"])
def test_capability_id_must_be_kebab_slug(bad):
    with pytest.raises(ValidationError):
        _capability(capability_id=bad)


# --- empty extraction / success-condition targets --------------------


def test_success_condition_target_cannot_be_empty():
    with pytest.raises(ValidationError):
        _capability(success_condition=SuccessCondition(
            strategy="next_cell", target="", description="d"))


def test_output_extraction_target_cannot_be_empty():
    with pytest.raises(ValidationError):
        _capability(outputs=[OutputSpec(
            name="x", type="str", description="d",
            extraction=Extraction(strategy="next_cell", target=""))])


# --- CapabilityStep action/value_template coherence -----------------


def test_type_step_requires_a_value_template():
    with pytest.raises(ValidationError, match="requires a value_template"):
        CapabilityStep(step_number=1, action="type",
                       target=Target(strategy="label", value="x"), value_template=None)


def test_click_step_must_not_carry_a_value_template():
    with pytest.raises(ValidationError, match="must not have a value_template"):
        CapabilityStep(step_number=1, action="click",
                       target=Target(strategy="label", value="x"), value_template="{p}")


# --- DESIGN NOTE: step_number is a label, replay iterates list order ---


def test_step_numbers_are_not_required_to_be_contiguous_or_monotonic():
    # JUDGMENT CALL: replay walks `capability.steps` in list order and never
    # reads `step_number` for sequencing (it is only echoed into evidence). A
    # non-contiguous set is therefore harmless and left un-validated; enforcing
    # 1..N here would reject legitimately hand-trimmed artifacts. Documented so
    # a future reader knows it is intentional, not an oversight.
    cap = _capability(steps=[_type_step(n=1), _click_step(n=7)])
    assert [s.step_number for s in cap.steps] == [1, 7]
