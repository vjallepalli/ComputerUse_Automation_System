"""`python -m replay` gates a DRAFT capability behind the human approval step,
via the SAME escalation mechanism a guardrail stop uses.

  * draft  -> request_replay_approval is called; resume runs this invocation,
              reject stops clean (exit 1), and NEITHER promotes the file.
  * approved -> the gate is skipped entirely; replay runs exactly as today.

request_replay_approval and replay_capability are mocked; playwright is faked.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import replay.cli as cli
from replay.cli import main
from schema.action import Target
from schema.capability import (
    ApprovalRecord, Capability, CapabilityStep, Extraction, OutputSpec, Parameter,
    SuccessCondition,
)
from schema.escalation import HandoffResult
from schema.replay import ReplayResult


def _capability(status="draft", approval=None) -> Capability:
    return Capability(
        capability_id="lookup-savings-balance", version="0.1.0",
        status=status, approval=approval,
        name="Look up savings balance", description="d", created_from_run="r",
        target_app="http://127.0.0.1:5001",
        parameters=[Parameter(name="member_number", type="int", description="d",
                              example="10003")],
        steps=[CapabilityStep(step_number=1, action="type",
                              target=Target(strategy="label", value="Member number"),
                              value_template="{member_number}")],
        outputs=[OutputSpec(name="savings_balance", type="str", description="d",
                            extraction=Extraction(strategy="next_cell", target="Savings"))],
        success_condition=SuccessCondition(strategy="next_cell", target="Savings",
                                           description="d"),
    )


# --- faked playwright (no browser) -----------------------------------


class _FakeLocator:
    def count(self):
        return 0            # _sign_on_if_needed sees no login form


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
def env(tmp_path, monkeypatch):
    import playwright.sync_api
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _FakeCM())

    replay_calls = {"n": 0}

    def fake_replay(capability, params, page, **kwargs):
        replay_calls["n"] += 1
        return ReplayResult(status="success", capability_id=capability.capability_id,
                            version=capability.version, params=params,
                            outputs={"savings_balance": "812.55"})

    monkeypatch.setattr(cli, "replay_capability", fake_replay)

    gate_calls = []

    def make_gate(action):
        def _gate(capability, *, run_id, **kwargs):
            gate_calls.append(SimpleNamespace(capability=capability, run_id=run_id))
            return HandoffResult(action=action, intervened=False, note="test")
        return _gate

    def _run(capability: Capability, gate_action="resume", extra=()):
        monkeypatch.setattr(cli, "request_replay_approval", make_gate(gate_action))
        cap_path = tmp_path / "cap.json"
        cap_path.write_text(capability.model_dump_json(indent=2))
        code = main([
            "--capability", str(cap_path), "--param", "member_number=10003",
            "--out-dir", str(tmp_path / "runs"), "--run-id", "gate-test", *extra,
        ])
        return SimpleNamespace(code=code, cap_path=cap_path,
                               replay_calls=replay_calls, gate_calls=gate_calls)

    return _run


# --- draft: the gate fires -----------------------------------------


def test_draft_replay_triggers_the_approval_gate(env, capsys):
    r = env(_capability(), gate_action="resume")

    assert len(r.gate_calls) == 1
    assert r.gate_calls[0].capability.capability_id == "lookup-savings-balance"
    assert r.replay_calls["n"] == 1                      # resume -> replay proceeded
    assert r.code == 0
    assert "continuing under human approval for this run only" in capsys.readouterr().out


def test_gate_reject_stops_before_replay_with_exit_1(env, capsys):
    r = env(_capability(), gate_action="reject")

    assert len(r.gate_calls) == 1
    assert r.replay_calls["n"] == 0                      # replay never started
    assert r.code == 1
    err = capsys.readouterr().err
    assert "this capability is a draft" in err
    assert "python -m agent.approve" in err
    assert "Traceback" not in err


def test_resume_runs_this_invocation_only_and_does_not_promote_the_file(env):
    r = env(_capability(), gate_action="resume")

    saved = Capability.model_validate_json(r.cap_path.read_text())
    assert saved.status == "draft"                       # NOT auto-promoted
    assert saved.approval is None


def test_reject_also_leaves_the_file_a_draft(env):
    r = env(_capability(), gate_action="reject")
    saved = Capability.model_validate_json(r.cap_path.read_text())
    assert saved.status == "draft"


def test_gate_is_checked_before_the_browser_launches(env, monkeypatch):
    # if the gate rejects, we must return before any playwright work
    import playwright.sync_api

    def _boom():
        raise AssertionError("browser must not launch when the draft gate rejects")

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", _boom)
    r = env(_capability(), gate_action="reject")
    assert r.code == 1
    assert r.replay_calls["n"] == 0


# --- approved: the gate is skipped -------------------------------


def test_approved_capability_skips_the_gate_entirely(env):
    approved = _capability(
        status="approved",
        approval=ApprovalRecord(approved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                note="reviewed"))
    r = env(approved, gate_action="reject")   # gate would reject IF it were consulted

    assert r.gate_calls == []                            # never consulted
    assert r.replay_calls["n"] == 1                      # replay ran as usual
    assert r.code == 0


def test_draft_gate_also_wraps_a_repeat_batch(env):
    r = env(_capability(), gate_action="resume", extra=("--repeat", "3"))
    assert len(r.gate_calls) == 1                        # one approval covers the batch
    assert r.replay_calls["n"] == 3
    assert r.code == 0
