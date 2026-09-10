"""Adversarial edge cases for the recorder's parameterisation heuristic.

`record_capability` turns a `type` step whose text appears verbatim in the goal
into a Parameter named from the field label. That's a heuristic with sharp
edges: substring coincidences, value collisions, type inference. Synthetic run
dirs (meta.json + steps.jsonl) are built per-test.

`test_two_type_steps_colliding_on_one_param_name_is_refused` is a regression for
a genuine silent-data-loss bug found in this pass.
"""

import json
from pathlib import Path

import pytest

from agent.record import RecorderError, record_capability

_OUTPUT_KW = dict(
    output_name="savings_balance",
    output_strategy="next_cell",
    output_target="Savings",
    capability_id="lookup-savings-balance",
    name="Look up savings balance",
    description="d",
)


def _write_run(tmp_path: Path, goal: str, steps: list[dict],
               start_url="http://127.0.0.1:5001/member") -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "meta.json").write_text(json.dumps({
        "run_id": "run", "goal": goal, "model": "claude-sonnet-5",
        "start_url": start_url, "exit_code": 0, "stop_reason": "done",
    }))
    with (run / "steps.jsonl").open("w") as fh:
        for i, s in enumerate(steps, 1):
            fh.write(json.dumps({"step": i, "decision": s}) + "\n")
        fh.write(json.dumps({"step": len(steps) + 1,
                             "decision": {"action": "done", "reason": "done"}}) + "\n")
    return run


def _type(label, text, strategy="label"):
    return {"action": "type", "target": {"strategy": strategy, "value": label}, "text": text}


def _click(value="Retrieve"):
    return {"action": "click", "target": {"strategy": "text_contains", "value": value}}


# --- substring coincidences between the goal and a typed value ----------


def test_typed_value_not_a_substring_of_the_goal_stays_literal(tmp_path):
    # goal mentions "member 100"; the field was typed "10003" -- "10003" is NOT
    # in "member 100", so it is treated as a fixed UI constant, not an input.
    # (A false negative for the heuristic; documented, and the safe direction --
    # a literal replays fine, it just isn't reusable.)
    run = _write_run(tmp_path, "Look up member 100 and read the balance",
                     [_type("Member number", "10003"), _click()])
    cap = record_capability(run, **_OUTPUT_KW)
    assert cap.parameters == []
    assert cap.steps[0].value_template == "10003"


def test_short_typed_value_coincidentally_in_the_goal_becomes_a_param(tmp_path):
    # goal has the literal "100"; the field typed exactly "100" -> substring
    # match -> parameterised. This is the heuristic's false-positive direction:
    # a coincidental short value gets promoted to an input.
    run = _write_run(tmp_path, "Look up member 100 and read the balance",
                     [_type("Member number", "100"), _click()])
    cap = record_capability(run, **_OUTPUT_KW)
    assert [p.name for p in cap.parameters] == ["member_number"]
    assert cap.steps[0].value_template == "{member_number}"


# --- value collisions -------------------------------------------------


def test_two_distinct_fields_with_the_same_typed_value_get_two_params(tmp_path):
    # both fields typed "100" but their labels differ -> two parameters, no
    # collision (the name is derived from the label, not the value).
    run = _write_run(tmp_path, "Set limit 100 and floor 100 for the account",
                     [_type("Limit", "100"), _type("Floor", "100"), _click()])
    cap = record_capability(run, **_OUTPUT_KW)
    assert sorted(p.name for p in cap.parameters) == ["floor", "limit"]
    assert [s.value_template for s in cap.steps[:2]] == ["{limit}", "{floor}"]


def test_two_type_steps_colliding_on_one_param_name_is_refused(tmp_path):
    # BUG (fixed): two steps whose targets snake_case to the SAME identifier but
    # with DIFFERENT recorded values used to silently keep the first and bind
    # the second to it, dropping a real input. Now it's a clear RecorderError.
    run = _write_run(tmp_path, "Enter Amount 100 then Amount 250 on the form",
                     [_type("Amount", "100"), _type("Amount", "250"), _click()])
    with pytest.raises(RecorderError, match="collapse to one name"):
        record_capability(run, **_OUTPUT_KW)


def test_same_field_same_value_typed_twice_is_not_a_collision(tmp_path):
    # idempotent: re-entering the identical value (model retyped) -> one param.
    run = _write_run(tmp_path, "Enter Amount 100 on the form",
                     [_type("Amount", "100"), _type("Amount", "100"), _click()])
    cap = record_capability(run, **_OUTPUT_KW)
    assert [p.name for p in cap.parameters] == ["amount"]
    assert [s.value_template for s in cap.steps[:2]] == ["{amount}", "{amount}"]


# --- type inference on the recorded example --------------------------


@pytest.mark.parametrize("text,expected", [
    ("10003", "int"),
    ("-3", "int"),
    ("812.55", "float"),
    ("-0.5", "float"),
    ("true", "bool"),
    ("FALSE", "bool"),
    ("5A", "str"),
    ("100.", "str"),        # not \d*\.\d+  -> str
    ("1,000", "str"),
])
def test_infer_type_from_the_goal_matched_value(tmp_path, text, expected):
    run = _write_run(tmp_path, f"Use value {text} in the Widget field",
                     [_type("Widget", text), _click()])
    cap = record_capability(run, **_OUTPUT_KW)
    assert cap.parameters[0].type == expected


# --- param name derivation ------------------------------------------


def test_param_name_from_a_role_text_target_strips_the_role_prefix(tmp_path):
    run = _write_run(tmp_path, "Type 10003 in the member box",
                     [_type("textbox:Member number", "10003", strategy="role_text"),
                      _click()])
    cap = record_capability(run, **_OUTPUT_KW)
    assert cap.parameters[0].name == "member_number"


def test_param_name_from_a_deduped_synthetic_label(tmp_path):
    run = _write_run(tmp_path, "Type 500 into Amount (1)",
                     [_type("Amount (1)", "500"), _click()])
    cap = record_capability(run, **_OUTPUT_KW)
    assert cap.parameters[0].name == "amount_1"
