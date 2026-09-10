"""`--repeat N` multi-run stability (brief section 8 stretch goal).

Two layers:
  * StabilityReport.from_runs -- the pure aggregate over N (result, duration)
    pairs. No mocks needed, just hand-built ReplayResults.
  * the CLI's --repeat wiring -- replay_capability is mocked to return canned
    results (playwright is faked so no browser launches); asserts per-run dirs,
    the stability_report.json, console lines, and exit codes.

replay/engine.py is untouched: --repeat only wraps repeated calls to the
existing replay_capability and aggregates.
"""

from types import SimpleNamespace

import playwright.sync_api
import pytest

import replay.cli as cli
from replay.cli import main
from schema.replay import (
    BusinessOutcome, FailureDetail, ReplayResult, StabilityReport,
)

_CAP = SimpleNamespace(capability_id="lookup-savings-balance", version="0.1.0")
_PARAMS = {"member_number": "10003"}


# --- canned ReplayResults -------------------------------------------------


def _success(outputs):
    return ReplayResult(status="success", capability_id=_CAP.capability_id,
                        version=_CAP.version, params=_PARAMS, outputs=outputs)


def _business(code):
    return ReplayResult(status="business_outcome", capability_id=_CAP.capability_id,
                        version=_CAP.version, params=_PARAMS,
                        business_outcome=BusinessOutcome(code=code, message="m"))


def _failure(step, msg):
    return ReplayResult(status="failure", capability_id=_CAP.capability_id,
                        version=_CAP.version, params=_PARAMS,
                        failure=FailureDetail(step_number=step, expected="e",
                                              observed="o", message=msg))


def _runs(results, durations=None):
    durations = durations or [1.0] * len(results)
    return list(zip(results, durations))


# --- StabilityReport.from_runs ------------------------------------------


def test_identical_outputs_across_every_run_is_flagged_stable():
    report = StabilityReport.from_runs(
        _CAP, _PARAMS, _runs([_success({"savings_balance": 812.55})] * 5))

    assert report.total_runs == 5
    assert report.status_counts == {"success": 5, "business_outcome": 0, "failure": 0}
    assert report.successful_runs == 5
    assert report.all_outputs_identical is True
    assert len(report.per_run) == 5
    assert report.per_run[0].run == 1
    assert report.per_run[0].key_output == "savings_balance=812.55"


def test_one_divergent_output_is_flagged_not_identical():
    results = [_success({"savings_balance": 812.55})] * 4 + [_success({"savings_balance": 999.99})]
    report = StabilityReport.from_runs(_CAP, _PARAMS, _runs(results))

    assert report.successful_runs == 5
    assert report.all_outputs_identical is False          # the real, important finding


def test_status_counts_for_a_mix_of_outcomes():
    results = [
        _success({"b": 1}), _success({"b": 1}), _success({"b": 1}),
        _business("restricted"), _business("not_found"),
        _failure(2, "did not reach the recorded end state"),
    ]
    report = StabilityReport.from_runs(_CAP, _PARAMS, _runs(results))

    assert report.status_counts == {"success": 3, "business_outcome": 2, "failure": 1}
    assert report.successful_runs == 3
    assert report.all_outputs_identical is True           # the 3 successes agree
    assert report.per_run[3].key_output == "restricted"
    assert report.per_run[5].key_output.startswith("step 2: ")


def test_outputs_identical_is_vacuously_true_below_two_successes():
    report = StabilityReport.from_runs(
        _CAP, _PARAMS, _runs([_success({"b": 1}), _failure(1, "x"), _failure(1, "x")]))
    assert report.successful_runs == 1
    assert report.all_outputs_identical is True

    none_ok = StabilityReport.from_runs(_CAP, _PARAMS, _runs([_failure(1, "x")]))
    assert none_ok.successful_runs == 0
    assert none_ok.all_outputs_identical is True


def test_duration_stats_min_max_mean():
    report = StabilityReport.from_runs(
        _CAP, _PARAMS,
        _runs([_success({"b": 1})] * 3, durations=[2.0, 6.0, 4.0]))
    assert report.duration_seconds.min == 2.0
    assert report.duration_seconds.max == 6.0
    assert report.duration_seconds.mean == 4.0


def test_from_runs_rejects_an_empty_batch():
    with pytest.raises(ValueError):
        StabilityReport.from_runs(_CAP, _PARAMS, [])


def test_report_round_trips_through_json():
    report = StabilityReport.from_runs(_CAP, _PARAMS, _runs([_success({"b": 1})] * 2))
    again = StabilityReport.model_validate_json(report.model_dump_json())
    assert again == report


# --- CLI --repeat wiring (replay_capability mocked, playwright faked) ----


def _capability_json() -> str:
    from schema.action import Target
    from schema.capability import (
        Capability, CapabilityStep, Extraction, OutputSpec, Parameter, SuccessCondition,
    )
    return Capability(
        capability_id="lookup-savings-balance", version="0.1.0",
        name="Look up savings balance", description="d", created_from_run="r",
        target_app="http://127.0.0.1:5001",
        parameters=[Parameter(name="member_number", type="int", description="d",
                              example="10003")],
        steps=[CapabilityStep(step_number=1, action="type",
                              target=Target(strategy="label", value="Member number"),
                              value_template="{member_number}")],
        outputs=[OutputSpec(name="savings_balance", type="float", description="d",
                            extraction=Extraction(strategy="next_cell", target="Savings"))],
        success_condition=SuccessCondition(strategy="next_cell", target="Savings",
                                           description="d"),
    ).model_dump_json()


class _FakeLocator:
    def count(self):
        return 0            # -> _sign_on_if_needed sees no login form, returns


class _FakePage:
    url = "http://127.0.0.1:5001/member"

    def goto(self, *_a, **_k):
        pass

    def locator(self, *_a, **_k):
        return _FakeLocator()


class _FakeContext:
    def new_page(self):
        return _FakePage()

    def close(self):
        pass


class _FakeBrowser:
    def new_context(self):
        return _FakeContext()

    def close(self):
        pass


class _FakePW:
    class chromium:
        @staticmethod
        def launch(**_k):
            return _FakeBrowser()


class _FakeCM:
    def __enter__(self):
        return _FakePW()

    def __exit__(self, *_a):
        return False


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _FakeCM())
    cap = tmp_path / "cap.json"
    cap.write_text(_capability_json())

    def _invoke(canned_results, extra_args=()):
        it = iter(canned_results)
        calls = {"n": 0}

        def fake_replay(capability, params, page, **kwargs):
            calls["n"] += 1
            return next(it)

        monkeypatch.setattr(cli, "replay_capability", fake_replay)
        out_dir = tmp_path / "runs"
        code = main([
            "--capability", str(cap), "--param", "member_number=10003",
            "--out-dir", str(out_dir), "--run-id", "batch", *extra_args,
        ])
        return code, out_dir / "batch", calls

    return _invoke


def test_default_single_run_writes_no_stability_report(cli_env, capsys):
    code, run_dir, calls = cli_env([_success({"savings_balance": 812.55})])

    assert code == 0
    assert calls["n"] == 1
    assert (run_dir / "result.json").is_file()          # unchanged single-run layout
    assert not (run_dir / "stability_report.json").exists()
    assert not (run_dir / "run-01").exists()
    assert "stability report" not in capsys.readouterr().out


def test_repeat_runs_n_times_writes_report_and_per_run_dirs(cli_env, capsys):
    outs = {"savings_balance": 812.55}
    code, run_dir, calls = cli_env([_success(outs), _success(outs), _success(outs)],
                                   extra_args=("--repeat", "3"))

    assert code == 0
    assert calls["n"] == 3
    for i in (1, 2, 3):
        assert (run_dir / f"run-{i:02d}" / "result.json").is_file()
        assert (run_dir / f"run-{i:02d}" / "steps.jsonl").is_file()

    report = StabilityReport.model_validate_json(
        (run_dir / "stability_report.json").read_text())
    assert report.total_runs == 3
    assert report.all_outputs_identical is True

    out = capsys.readouterr().out
    assert "run 1/3" in out and "run 3/3" in out
    assert "IDENTICAL" in out


def test_repeat_with_divergent_outputs_exits_nonzero_and_flags_it(cli_env, capsys):
    code, run_dir, _ = cli_env(
        [_success({"savings_balance": 812.55}),
         _success({"savings_balance": 812.55}),
         _success({"savings_balance": 42.00})],
        extra_args=("--repeat", "3"))

    assert code == 1                                    # non-determinism -> nonzero exit
    report = StabilityReport.model_validate_json(
        (run_dir / "stability_report.json").read_text())
    assert report.all_outputs_identical is False
    assert report.status_counts["success"] == 3

    out = capsys.readouterr().out
    assert "DIVERGED" in out
    assert "not deterministic" in out


def test_repeat_with_a_failing_run_exits_nonzero(cli_env):
    code, run_dir, _ = cli_env(
        [_success({"b": 1}), _failure(2, "did not reach the recorded end state"),
         _success({"b": 1})],
        extra_args=("--repeat", "3"))

    assert code == 1
    report = StabilityReport.model_validate_json(
        (run_dir / "stability_report.json").read_text())
    assert report.status_counts == {"success": 2, "business_outcome": 0, "failure": 1}


def test_repeat_all_business_outcomes_is_a_clean_zero_exit(cli_env):
    code, run_dir, _ = cli_env([_business("restricted")] * 2, extra_args=("--repeat", "2"))
    assert code == 0                                    # business_outcome is legitimate
    report = StabilityReport.model_validate_json(
        (run_dir / "stability_report.json").read_text())
    assert report.status_counts["business_outcome"] == 2
    assert report.successful_runs == 0


def test_repeat_zero_is_rejected(cli_env, capsys):
    code, _run_dir, calls = cli_env([], extra_args=("--repeat", "0"))
    assert code == 1
    assert calls["n"] == 0
    assert "--repeat must be >= 1" in capsys.readouterr().err
