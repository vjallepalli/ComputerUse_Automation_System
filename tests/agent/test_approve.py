"""`python -m agent.approve` -- the deliberate draft -> approved promotion, plus
the derived stability signal shown at approval time.

The capability file is built on disk; the CLI reads it, prints a summary +
signal, and (if draft) writes status="approved" + an ApprovalRecord back.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from agent.approve import main
from schema.action import Target
from schema.capability import (
    ApprovalRecord, Capability, CapabilityStep, Extraction, OutputSpec, Parameter,
    SuccessCondition,
)
from schema.replay import ReplayResult, StabilityReport


def _capability(status="draft", approval=None) -> Capability:
    return Capability(
        capability_id="lookup-savings-balance", version="0.1.0",
        status=status, approval=approval,
        name="Look up savings balance",
        description="Look up a member by number and read their savings balance.",
        created_from_run="run-1", target_app="http://127.0.0.1:5001",
        parameters=[Parameter(name="member_number", type="int", description="the id",
                              example="10003")],
        steps=[CapabilityStep(step_number=1, action="type",
                              target=Target(strategy="label", value="Member number"),
                              value_template="{member_number}")],
        outputs=[OutputSpec(name="savings_balance", type="float", description="the balance",
                            extraction=Extraction(strategy="next_cell", target="Savings"))],
        success_condition=SuccessCondition(strategy="next_cell", target="Savings",
                                           description="d"),
    )


def _write(tmp_path, capability: Capability):
    path = tmp_path / "cap.json"
    path.write_text(capability.model_dump_json(indent=2) + "\n")
    return path


def _stability_report(tmp_path, *, capability_id="lookup-savings-balance",
                      version="0.1.0", outputs_per_run):
    """outputs_per_run: list of dicts (a success run each) -- write a report the
    approve CLI will discover under its --evidence-dir."""
    runs = [
        (ReplayResult(status="success", capability_id=capability_id, version=version,
                      params={"member_number": "10003"}, outputs=o), 1.0)
        for o in outputs_per_run
    ]
    cap = SimpleNamespace(capability_id=capability_id, version=version)
    report = StabilityReport.from_runs(cap, {"member_number": "10003"}, runs)
    batch = tmp_path / "evidence" / "batch-01"
    batch.mkdir(parents=True)
    (batch / "stability_report.json").write_text(report.model_dump_json(indent=2))
    return tmp_path / "evidence"


# --- draft -> approved --------------------------------------------------


def test_draft_is_promoted_to_approved_with_a_record(tmp_path, capsys):
    path = _write(tmp_path, _capability())
    before = datetime.now(timezone.utc)

    code = main(["--capability", str(path), "--note", "reviewed the 5-run stability report",
                 "--evidence-dir", str(tmp_path / "nope")])
    assert code == 0

    saved = Capability.model_validate_json(path.read_text())
    assert saved.status == "approved"
    assert saved.approval is not None
    assert saved.approval.note == "reviewed the 5-run stability report"
    assert saved.approval.approved_at >= before
    assert "approved" in capsys.readouterr().out


def test_approve_with_no_note_leaves_note_none(tmp_path):
    path = _write(tmp_path, _capability())
    assert main(["--capability", str(path), "--evidence-dir", str(tmp_path / "x")]) == 0
    saved = Capability.model_validate_json(path.read_text())
    assert saved.status == "approved"
    assert saved.approval.note is None


def test_reapproving_an_approved_capability_is_a_clean_noop(tmp_path, capsys):
    approved = _capability(
        status="approved",
        approval=ApprovalRecord(approved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                                note="original approval"))
    path = _write(tmp_path, approved)
    original_bytes = path.read_text()

    code = main(["--capability", str(path), "--note", "second time",
                 "--evidence-dir", str(tmp_path / "x")])

    assert code == 0                                   # informational, not an error
    out = capsys.readouterr().out
    assert "already approved" in out
    assert "original approval" in out
    assert path.read_text() == original_bytes          # file untouched, note not overwritten


# --- clean errors ----------------------------------------------------


def test_missing_file_is_a_clean_error(tmp_path, capsys):
    code = main(["--capability", str(tmp_path / "nope.json")])
    assert code == 1
    err = capsys.readouterr().err
    assert "cannot read capability file" in err
    assert "Traceback" not in err


def test_malformed_capability_is_a_clean_error(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    code = main(["--capability", str(path)])
    assert code == 1
    err = capsys.readouterr().err
    assert "invalid capability artifact" in err
    assert "Traceback" not in err


# --- stability signal at approval time ------------------------------


def test_summary_shows_the_stability_signal_when_reports_exist(tmp_path, capsys):
    path = _write(tmp_path, _capability())
    evidence = _stability_report(
        tmp_path, outputs_per_run=[{"savings_balance": 812.55}] * 5)

    main(["--capability", str(path), "--evidence-dir", str(evidence)])

    out = capsys.readouterr().out
    assert "stability:" in out
    assert "100% of successful runs returned identical outputs" in out
    assert "no stability data yet" not in out


def test_summary_flags_divergence_in_the_stability_signal(tmp_path, capsys):
    path = _write(tmp_path, _capability())
    evidence = _stability_report(
        tmp_path,
        outputs_per_run=[{"savings_balance": 812.55}, {"savings_balance": 812.55},
                         {"savings_balance": 999.99}])

    main(["--capability", str(path), "--evidence-dir", str(evidence)])

    out = capsys.readouterr().out
    assert "67% of successful runs returned identical outputs" in out


def test_summary_says_no_data_when_no_reports_exist(tmp_path, capsys):
    path = _write(tmp_path, _capability())
    code = main(["--capability", str(path), "--evidence-dir", str(tmp_path / "empty")])
    assert code == 0
    out = capsys.readouterr().out
    assert "no stability data yet" in out
    # never a fabricated percentage
    assert "% of successful runs" not in out


def test_reports_for_a_different_capability_are_ignored(tmp_path, capsys):
    path = _write(tmp_path, _capability())
    evidence = _stability_report(
        tmp_path, capability_id="some-other-capability",
        outputs_per_run=[{"x": 1}] * 3)

    main(["--capability", str(path), "--evidence-dir", str(evidence)])
    assert "no stability data yet" in capsys.readouterr().out
