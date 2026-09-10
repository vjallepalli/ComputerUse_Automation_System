"""`python -m replay` -- replay a saved Capability artifact against the target app.

    python -m replay \\
        --capability artifacts/capabilities/lookup-savings-balance-v0.1.0.json \\
        --param member_number=10002 --headed

Prints the ReplayResult as readable text and writes the full result plus a
step-by-step log to evidence/runs/<run-id>/ (the evidence path from CLAUDE.md's
table -- gitignored). Exit 0 for success or business_outcome (both are
legitimate results), 1 for failure or a params mismatch.

`--repeat N` (brief section 8 stretch goal): replay the SAME capability + params
N times, then write a StabilityReport (schema/replay.py) to
evidence/runs/<batch-id>/stability_report.json alongside the per-run dirs
(run-01/, run-02/, ...). The headline signal is whether every successful run
extracted byte-identical outputs -- replay is meant to be deterministic. Exit 1
if any run failed OR the outputs were not all identical. Default N=1 is the
existing single-run behaviour, unchanged, and writes no stability report.

Approval gate (brief section 8 stretch goal): a capability with
status="draft" is not cleared for UNATTENDED replay. Before any browser work,
it escalates to a human via the same pause/resume mechanism a guardrail stop
uses -- resume runs this invocation only (no promotion), reject stops with
exit 1. `python -m agent.approve` is the deliberate draft -> approved step. An
already-approved capability skips the gate: no behaviour change.

`replay_capability` guarantees "any escaped exception -> ReplayResult(failure)",
but that only covers the engine. CLI-level SETUP -- browser launch, initial
navigation, sign-on -- runs before the engine is called, so main() adds an OUTER
try/except over that path: any exception there becomes a failure ReplayResult
(step_number -1), written to evidence and printed like any other failure, exit 1.
The traceback goes to evidence/runs/<run-id>/error.txt, never the terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from pydantic import ValidationError

from escalation.handoff import request_replay_approval
from guardrail.policy import resolve_max_auto_tier
from replay.engine import DEFAULT_RETRY_WAIT_S, default_run_id, replay_capability
from schema.capability import Capability
from schema.guardrail import RiskTier
from schema.replay import FailureDetail, ReplayResult, StabilityReport


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m replay",
        description="Deterministically replay a Capability artifact (no LLM).",
    )
    parser.add_argument("--capability", required=True, help="path to the capability JSON")
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                        help="a capability parameter; repeatable")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--run-id", default=None, help="default: UTC timestamp")
    parser.add_argument("--out-dir", default="evidence/runs")
    parser.add_argument("--retry-wait", type=float, default=DEFAULT_RETRY_WAIT_S,
                        help="seconds to wait before the single per-step retry")
    parser.add_argument("--max-auto-tier", choices=[t.value for t in RiskTier], default=None,
                        help="highest risk tier to auto-execute; default $AGENT_MAX_AUTO_RISK_TIER")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="replay N times and write a stability report (default 1)")
    args = parser.parse_args(argv)

    if args.repeat < 1:
        print(f"--repeat must be >= 1, got {args.repeat}", file=sys.stderr)
        return 1

    max_auto_tier = RiskTier(args.max_auto_tier) if args.max_auto_tier else resolve_max_auto_tier()

    # The capability file is loaded BEFORE the browser/engine, so it sits
    # outside the setup try/except below and has no run_dir / Capability to hang
    # a ReplayResult on yet. A missing file or a hand-broken artifact must still
    # give one clear line, never a raw traceback (same contract as the rest of
    # this CLI). Regression: tests/replay/test_cli.py.
    try:
        raw_capability = Path(args.capability).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read capability file {args.capability!r}: {exc}", file=sys.stderr)
        return 1
    try:
        capability = Capability.model_validate_json(raw_capability)
    except ValidationError as exc:
        print(
            f"invalid capability artifact {args.capability!r}: "
            f"{exc.error_count()} validation error(s) -- {_short(str(exc))}",
            file=sys.stderr,
        )
        return 1
    params = _parse_params(args.param)

    run_id = args.run_id or default_run_id()
    run_dir = Path(args.out_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"replay {capability.capability_id} v{capability.version}  "
          f"(max auto risk tier = {max_auto_tier.value})")

    # --- approval gate: a draft capability is not cleared for UNATTENDED
    # replay. Route it through the SAME pause/resume mechanism a guardrail stop
    # uses (request_replay_approval, adapted for "no page yet" -- see its
    # docstring). resume => run this invocation only; reject => stop cleanly.
    # An approved capability skips this entirely: no behaviour change. One
    # approval covers the whole invocation, including a --repeat batch; it does
    # NOT promote the file (that's `python -m agent.approve`).
    if capability.status == "draft":
        gate = request_replay_approval(capability, run_id=run_id)
        if gate.action == "reject":
            print("\nreplay stopped: this capability is a draft and was not "
                  "cleared for unattended replay.", file=sys.stderr)
            print(f"  approve it:  python -m agent.approve --capability "
                  f"{args.capability}", file=sys.stderr)
            return 1
        print("\ncontinuing under human approval for this run only; the "
              "capability stays 'draft' (approve it with `python -m agent.approve` "
              "to replay it unattended).\n")

    # --- single run: unchanged behaviour, no stability report -------------
    if args.repeat == 1:
        result = _replay_once(capability, params, headed=args.headed,
                              retry_wait=args.retry_wait, max_auto_tier=max_auto_tier,
                              run_id=run_id, run_dir=run_dir)
        _print(result, run_dir)
        return 0 if result.status in ("success", "business_outcome") else 1

    # --- multi-run stability: N identical invocations --------------------
    print(f"stability: replaying {args.repeat}x with the same params {params}\n")
    runs: list[tuple[ReplayResult, float]] = []
    for i in range(1, args.repeat + 1):
        sub_dir = run_dir / f"run-{i:02d}"
        started = time.perf_counter()
        result = _replay_once(capability, params, headed=args.headed,
                              retry_wait=args.retry_wait, max_auto_tier=max_auto_tier,
                              run_id=f"{run_id}-{i:02d}", run_dir=sub_dir)
        elapsed = time.perf_counter() - started
        runs.append((result, elapsed))
        print(f"  run {i}/{args.repeat}: {_one_line(result)}  ({elapsed:.2f}s)")

    report = StabilityReport.from_runs(capability, params, runs)
    (run_dir / "stability_report.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _print_stability(report, run_dir)
    ok = report.status_counts["failure"] == 0 and report.all_outputs_identical
    return 0 if ok else 1


def _replay_once(capability, params: dict, *, headed: bool, retry_wait: float,
                 max_auto_tier, run_id: str, run_dir: Path) -> ReplayResult:
    """One full replay against a fresh browser. Writes result.json + steps.jsonl
    (+ error.txt on a CLI-setup failure) into `run_dir`. Never raises -- always
    returns a ReplayResult, exactly as the single-run path always has."""
    run_dir.mkdir(parents=True, exist_ok=True)
    step_log: list[dict] = []
    result: ReplayResult
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = context = None
            try:
                browser = pw.chromium.launch(headless=not headed)
                context = browser.new_context()
                page = context.new_page()
                _sign_on_if_needed(page, capability.target_app)
                result = replay_capability(
                    capability, params, page,
                    retry_wait=retry_wait, on_step=step_log.append,
                    max_auto_tier=max_auto_tier, run_id=run_id,
                )
            finally:
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()
    except Exception as exc:
        # CLI-level SETUP failure -- browser launch / initial navigation /
        # sign-on -- BEFORE replay_capability's own per-step backstop runs.
        # Convert to a proper failure ReplayResult; traceback -> evidence, not
        # the terminal. Does not weaken the engine's internal backstop.
        (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        step_log.append({
            "stage": "cli-setup", "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback_file": "error.txt",
        })
        result = ReplayResult.failed(capability, params, FailureDetail(
            step_number=-1,
            expected="launch the browser, sign on to the target app, and start replay",
            observed=_short(f"{type(exc).__name__}: {exc}"),
            message=(
                "replay could not start: CLI setup failed before the engine "
                "(target app unreachable, browser launch failed, or sign-on failed) "
                "-- full traceback in error.txt"
            ),
        ))

    # evidence/runs/ is gitignored. TODO(redaction, CLAUDE.md rule 1): route
    # params / outputs through the /evidence redactor once that module exists.
    (run_dir / "result.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    with (run_dir / "steps.jsonl").open("w", encoding="utf-8") as fh:
        for record in step_log:
            fh.write(json.dumps(record) + "\n")
    return result


def _one_line(result: ReplayResult) -> str:
    """A single terminal line for one run in a --repeat batch."""
    if result.status == "success":
        outs = ", ".join(f"{k}={v!r}" for k, v in (result.outputs or {}).items())
        return f"success       {outs}" if outs else "success       (no outputs)"
    if result.status == "business_outcome":
        return f"business      {result.business_outcome.code}"
    f = result.failure
    return f"FAILURE       step {f.step_number}: {_short(f.message, 120)}"


def _print_stability(report: StabilityReport, run_dir: Path) -> None:
    counts = report.status_counts
    d = report.duration_seconds
    print("\n" + "-" * 60)
    print(f"stability report  ({report.total_runs} runs of "
          f"{report.capability_id} v{report.version})")
    print(f"  status:    success={counts['success']}  "
          f"business_outcome={counts['business_outcome']}  failure={counts['failure']}")
    if report.successful_runs >= 2:
        verdict = "IDENTICAL" if report.all_outputs_identical else "*** DIVERGED ***"
        print(f"  outputs:   {verdict} across {report.successful_runs} successful runs")
    elif report.successful_runs == 1:
        print("  outputs:   only 1 successful run -- nothing to compare")
    else:
        print("  outputs:   no successful runs")
    print(f"  duration:  min {d.min:.2f}s  mean {d.mean:.2f}s  max {d.max:.2f}s")
    if not report.all_outputs_identical:
        print("  !! successful runs did not all extract the same outputs -- "
              "replay is not deterministic here")
    print(f"  report:    {run_dir / 'stability_report.json'}")
    print("-" * 60)


def _short(text: str, limit: int = 300) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."


def _sign_on_if_needed(page, target_app: str) -> None:
    """Programmatic sign-on if the app bounces us to the login form -- the same
    convenience the discovery orchestrator uses. Credentials aren't part of the
    capability; TARGET_APP_USERNAME / TARGET_APP_PASSWORD, default clerk / vault.
    """
    import os

    page.goto(target_app)
    if not page.locator("input[name='u']").count():
        return
    page.fill("input[name='u']", os.environ.get("TARGET_APP_USERNAME") or "clerk")
    page.fill("input[name='p']", os.environ.get("TARGET_APP_PASSWORD") or "vault")
    page.click("input[value='Sign On']")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _parse_params(pairs: list[str]) -> dict:
    out: dict = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"--param must be NAME=VALUE, got {item!r}")
        name, _, value = item.partition("=")
        out[name.strip()] = value
    return out


def _print(result, run_dir: Path) -> None:
    print(f"status:     {result.status}")
    print(f"capability: {result.capability_id} v{result.version}")
    print(f"params:     {result.params}")
    if result.status == "success":
        for name, value in (result.outputs or {}).items():
            print(f"output:     {name} = {value!r}")
    elif result.status == "business_outcome":
        bo = result.business_outcome
        print(f"business:   {bo.code}  ({bo.message})")
    else:
        f = result.failure
        print(f"failure at step {f.step_number}")
        print(f"  expected: {f.expected}")
        print(f"  observed: {f.observed}")
        print(f"  message:  {f.message}")
    print(f"evidence:   {run_dir}/")


if __name__ == "__main__":
    sys.exit(main())
