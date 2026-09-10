"""record_capability + CLI, against a fixture 3-step run (type 10003, click Retrieve, done)."""

import json
import shutil
from pathlib import Path

import pytest

from agent.record import RecorderError, main, record_capability, write_capability
from schema.capability import Capability

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "run_lookup_10003"

_OUTPUT_KW = dict(
    output_name="savings_balance",
    output_strategy="text_contains",
    output_target="Savings",
    capability_id="lookup-savings-balance",
    name="Look up savings balance",
    description="Look up a member by number and return their savings balance.",
)


def _run_dir_with_exit_code(tmp_path: Path, code: int) -> Path:
    dst = tmp_path / "run"
    shutil.copytree(FIXTURE, dst)
    meta = json.loads((dst / "meta.json").read_text())
    meta["exit_code"] = code
    meta["stop_reason"] = "reached max steps (25) without a done action"
    (dst / "meta.json").write_text(json.dumps(meta))
    return dst


# --- parameterisation heuristic ------------------------------------------


def test_records_goal_value_as_parameter_and_fixed_value_as_literal():
    cap = record_capability(FIXTURE, **_OUTPUT_KW)

    # "10003" is in the goal -> a parameter, named from the field label
    assert [p.name for p in cap.parameters] == ["member_number"]
    param = cap.parameters[0]
    assert param.example == "10003"
    assert param.type == "int"  # all-digits heuristic

    # step 1: the typed literal is replaced by the {param} reference
    assert cap.steps[0].action == "type"
    assert cap.steps[0].value_template == "{member_number}"
    assert cap.steps[0].target.strategy == "label"
    assert cap.steps[0].target.value == "Member number"  # carried over verbatim

    # step 2: "Retrieve" is NOT in the goal -> stays a literal click, no param
    assert cap.steps[1].action == "click"
    assert cap.steps[1].value_template is None
    assert cap.steps[1].target.strategy == "text_contains"
    assert cap.steps[1].target.value == "Retrieve"

    # the terminating 'done' step is not a capability step
    assert [s.step_number for s in cap.steps] == [1, 2]


def test_output_and_success_condition_come_from_the_reviewed_args():
    cap = record_capability(FIXTURE, **_OUTPUT_KW)

    assert cap.outputs[0].name == "savings_balance"
    assert cap.outputs[0].extraction.strategy == "text_contains"
    assert cap.outputs[0].extraction.target == "Savings"

    # success_condition defaults to the output's own selector
    assert cap.success_condition.strategy == "text_contains"
    assert cap.success_condition.target == "Savings"


def test_provenance_and_versions():
    cap = record_capability(FIXTURE, **_OUTPUT_KW)
    assert cap.created_from_run == "run_lookup_10003"
    assert cap.target_app == "http://127.0.0.1:5001"  # base URL, not the /member path
    assert cap.version == "0.1.0"
    assert cap.schema_version == "1.0"


# --- refuses a run that did not succeed ---------------------------------


def test_refuses_incomplete_run(tmp_path):
    bad = _run_dir_with_exit_code(tmp_path, code=1)
    with pytest.raises(RecorderError, match="exit_code"):
        record_capability(bad, **_OUTPUT_KW)


# --- JSON round-trip --------------------------------------------------


def test_capability_json_round_trips():
    cap = record_capability(FIXTURE, **_OUTPUT_KW)
    restored = Capability.model_validate_json(cap.model_dump_json())
    assert restored == cap
    # and plain dict round-trip
    assert Capability.model_validate(json.loads(cap.model_dump_json())) == cap


# --- CLI --------------------------------------------------------------


def test_cli_writes_artifact_at_expected_path(tmp_path, capsys):
    out_dir = tmp_path / "capabilities"
    code = main([
        "--run", str(FIXTURE),
        "--capability-id", "lookup-savings-balance",
        "--name", "Look up savings balance",
        "--description", "Look up a member and return their savings balance.",
        "--output-name", "savings_balance",
        "--output-strategy", "text_contains",
        "--output-target", "Savings",
        "--out-dir", str(out_dir),
    ])
    assert code == 0

    written = out_dir / "lookup-savings-balance-v0.1.0.json"
    assert written.is_file()
    cap = Capability.model_validate_json(written.read_text())
    assert cap.capability_id == "lookup-savings-balance"
    assert [p.name for p in cap.parameters] == ["member_number"]
    assert "wrote" in capsys.readouterr().out


def test_cli_reports_failure_on_incomplete_run(tmp_path, capsys):
    bad = _run_dir_with_exit_code(tmp_path, code=1)
    code = main([
        "--run", str(bad),
        "--capability-id", "x", "--name", "X", "--description", "d",
        "--output-name", "y", "--output-strategy", "text_contains",
        "--output-target", "Savings",
        "--out-dir", str(tmp_path / "caps"),
    ])
    assert code == 1
    assert "cannot record capability" in capsys.readouterr().err
    assert not (tmp_path / "caps").exists()
