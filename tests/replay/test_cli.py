"""replay CLI: a setup failure (target app down, browser launch fails, sign-on
fails) becomes a failure ReplayResult, not a raw traceback. Playwright is faked.

The `*_capability_file_*` cases at the bottom were added in the adversarial pass:
the capability file is loaded before the setup try/except, so a missing or
hand-broken artifact used to dump a raw traceback -- now one clean line, exit 1.
"""

import playwright.sync_api

from replay.cli import main
from schema.action import Target
from schema.capability import (
    Capability, CapabilityStep, Extraction, OutputSpec, Parameter, SuccessCondition,
)
from schema.replay import ReplayResult


def _capability_json() -> str:
    return Capability(
        capability_id="lookup-savings-balance", version="0.1.0",
        name="Look up savings balance", description="d", created_from_run="r",
        target_app="http://127.0.0.1:5001",
        parameters=[Parameter(name="member_number", type="int", description="d", example="10003")],
        steps=[CapabilityStep(step_number=1, action="type",
                              target=Target(strategy="label", value="Member number"),
                              value_template="{member_number}")],
        outputs=[OutputSpec(name="savings_balance", type="str", description="d",
                            extraction=Extraction(strategy="next_cell", target="Savings"))],
        success_condition=SuccessCondition(strategy="next_cell", target="Savings", description="d"),
    ).model_dump_json()


class _FakePage:
    """goto() raises like Playwright does when the target app is unreachable."""

    def goto(self, *_a, **_k):
        raise RuntimeError(
            "Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:5001/\n"
            "Call log:\n  - navigating to \"http://127.0.0.1:5001/\""
        )


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


class _FakeSyncPlaywrightCM:
    def __enter__(self):
        return _FakePW()

    def __exit__(self, *_a):
        return False


def _run(monkeypatch, tmp_path, sync_playwright_factory):
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", sync_playwright_factory)
    cap = tmp_path / "cap.json"
    cap.write_text(_capability_json())
    out_dir = tmp_path / "evidence"
    code = main([
        "--capability", str(cap),
        "--param", "member_number=10003",
        "--out-dir", str(out_dir),
        "--run-id", "testrun",
    ])
    run_dir = out_dir / "testrun"
    return code, run_dir


def test_connection_refused_during_sign_on_becomes_a_failure_result(tmp_path, monkeypatch, capsys):
    code, run_dir = _run(tmp_path=tmp_path, monkeypatch=monkeypatch,
                         sync_playwright_factory=_FakeSyncPlaywrightCM)

    assert code == 1                                        # the CLI's defined failure code

    result = ReplayResult.model_validate_json((run_dir / "result.json").read_text())
    assert result.status == "failure"
    assert result.failure.step_number == -1
    assert "CONNECTION_REFUSED" in result.failure.observed
    assert "CLI setup failed" in result.failure.message

    # traceback -> evidence, NOT the terminal
    assert (run_dir / "error.txt").read_text().startswith("Traceback (most recent call last)")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback (most recent call last)" not in combined
    assert "failure at step -1" in combined                # printed like any failure

    # the setup failure is also in the step log
    steps = [line for line in (run_dir / "steps.jsonl").read_text().splitlines() if line]
    assert any('"stage": "cli-setup"' in s for s in steps)


def test_browser_launch_failure_becomes_a_failure_result(tmp_path, monkeypatch, capsys):
    class _LaunchFailsPW:
        class chromium:
            @staticmethod
            def launch(**_k):
                raise RuntimeError("Executable doesn't exist -- run `playwright install`")

    class _CM:
        def __enter__(self):
            return _LaunchFailsPW()

        def __exit__(self, *_a):
            return False

    code, run_dir = _run(tmp_path=tmp_path, monkeypatch=monkeypatch,
                         sync_playwright_factory=_CM)

    assert code == 1
    result = ReplayResult.model_validate_json((run_dir / "result.json").read_text())
    assert result.status == "failure"
    assert "playwright install" in result.failure.observed
    assert "Traceback" not in (capsys.readouterr().out)


# --- capability file: missing / malformed / schema-invalid ----------------
# These fail before any browser work, so no playwright fake is needed.


def _no_traceback(capsys):
    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert "Traceback (most recent call last)" not in text
    return text


def test_missing_capability_file_is_a_clean_error(tmp_path, capsys):
    code = main(["--capability", str(tmp_path / "nope.json"), "--param", "member_number=1"])
    assert code == 1
    assert "cannot read capability file" in _no_traceback(capsys)


def test_capability_path_is_a_directory_is_a_clean_error(tmp_path, capsys):
    code = main(["--capability", str(tmp_path), "--param", "member_number=1"])
    assert code == 1
    _no_traceback(capsys)


def test_malformed_json_capability_is_a_clean_error(tmp_path, capsys):
    cap = tmp_path / "cap.json"
    cap.write_text("{ this is not valid json ")
    code = main(["--capability", str(cap), "--param", "member_number=1"])
    assert code == 1
    assert "invalid capability artifact" in _no_traceback(capsys)


def test_schema_invalid_capability_is_a_clean_error(tmp_path, capsys):
    # valid JSON, but missing required fields / wrong types for the schema
    cap = tmp_path / "cap.json"
    cap.write_text('{"capability_id": "x", "version": "not-semver", "steps": []}')
    code = main(["--capability", str(cap), "--param", "member_number=1"])
    assert code == 1
    text = _no_traceback(capsys)
    assert "invalid capability artifact" in text
    assert "validation error" in text
