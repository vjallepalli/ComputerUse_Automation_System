"""replay CLI: a setup failure (target app down, browser launch fails, sign-on
fails) becomes a failure ReplayResult, not a raw traceback. Playwright is faked.

The `*_capability_file_*` cases at the bottom were added in the adversarial pass:
the capability file is loaded before the setup try/except, so a missing or
hand-broken artifact used to dump a raw traceback -- now one clean line, exit 1.

`test_main_loads_dotenv_before_sign_on_*` is a regression for a genuine bug: this
CLI read TARGET_APP_USERNAME/PASSWORD straight from os.environ with no
load_dotenv() call (same root cause as target_app/__main__.py, see
tests/target_app/test_env_loading.py) -- a value set only in .env was silently
ignored during sign-on.
"""

import os
from datetime import datetime, timezone

import dotenv
import playwright.sync_api
import pytest

from replay.cli import main
from schema.action import Target
from schema.capability import (
    ApprovalRecord, Capability, CapabilityStep, Extraction, OutputSpec, Parameter,
    SuccessCondition,
)
from schema.replay import ReplayResult


def _capability_json() -> str:
    # status="approved" so these CLI tests exercise the replay flow itself, not
    # the draft approval gate (that gate has its own tests in
    # tests/replay/test_approval_gate.py).
    return Capability(
        capability_id="lookup-savings-balance", version="0.1.0",
        status="approved",
        approval=ApprovalRecord(approved_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
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


# --- BUG regression: .env-only credentials were ignored during sign-on -----


@pytest.fixture
def isolated_environ():
    """load_dotenv() mutates os.environ directly, bypassing monkeypatch's
    setenv/delenv tracking -- snapshot + restore by hand."""
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


class _LoginFormPage:
    """A login form IS present (unlike _FakePage above), so
    _sign_on_if_needed actually reads TARGET_APP_USERNAME/PASSWORD and fills
    the form -- the exact code path the bug was in."""

    def __init__(self):
        self.url = "http://127.0.0.1:5001/login"
        self.filled: dict[str, str] = {}

    def goto(self, *_a, **_k):
        pass

    def locator(self, selector):
        class _Loc:
            def count(self_inner):
                return 1  # a login form is present

        return _Loc()

    def fill(self, selector, value):
        self.filled[selector] = value

    def click(self, *_a, **_k):
        pass

    def wait_for_load_state(self, *_a, **_k):
        pass


def test_main_loads_dotenv_before_sign_on_reads_credentials(
    tmp_path, monkeypatch, isolated_environ
):
    env_file = tmp_path / ".env"
    env_file.write_text("TARGET_APP_USERNAME=opuser\nTARGET_APP_PASSWORD=s3cret\n")
    os.environ.pop("TARGET_APP_USERNAME", None)
    os.environ.pop("TARGET_APP_PASSWORD", None)
    real_load_dotenv = dotenv.load_dotenv
    monkeypatch.setattr(dotenv, "load_dotenv",
                        lambda *a, **k: real_load_dotenv(dotenv_path=env_file))

    login_page = _LoginFormPage()

    class _Context:
        def new_page(self):
            return login_page

        def close(self):
            pass

    class _Browser:
        def new_context(self):
            return _Context()

        def close(self):
            pass

    class _PW:
        class chromium:
            @staticmethod
            def launch(**_k):
                return _Browser()

    class _CM:
        def __enter__(self):
            return _PW()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _CM())
    monkeypatch.setattr(
        "replay.cli.replay_capability",
        lambda capability, params, page, **kwargs: ReplayResult(
            status="success", capability_id=capability.capability_id,
            version=capability.version, params=params, outputs={"savings_balance": "812.55"}),
    )

    cap = tmp_path / "cap.json"
    cap.write_text(_capability_json())
    code = main([
        "--capability", str(cap), "--param", "member_number=10003",
        "--out-dir", str(tmp_path / "evidence"), "--run-id", "testrun",
    ])

    assert code == 0
    # the bug: without load_dotenv(), these would be the hardcoded "clerk"/"vault"
    assert login_page.filled["input[name='u']"] == "opuser"
    assert login_page.filled["input[name='p']"] == "s3cret"
