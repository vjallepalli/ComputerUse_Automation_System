"""`python -m replay` -- replay a saved Capability artifact against the target app.

    python -m replay \\
        --capability artifacts/capabilities/lookup-savings-balance-v0.1.0.json \\
        --param member_number=10002 --headed

Prints the ReplayResult as readable text and writes the full result plus a
step-by-step log to evidence/runs/<run-id>/ (the evidence path from CLAUDE.md's
table -- gitignored). Exit 0 for success or business_outcome (both are
legitimate results), 1 for failure or a params mismatch.

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
import traceback
from datetime import datetime, timezone
from pathlib import Path

from guardrail.policy import resolve_max_auto_tier
from replay.engine import DEFAULT_RETRY_WAIT_S, replay_capability
from schema.capability import Capability
from schema.guardrail import RiskTier
from schema.replay import FailureDetail, ReplayResult


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
    args = parser.parse_args(argv)

    max_auto_tier = RiskTier(args.max_auto_tier) if args.max_auto_tier else resolve_max_auto_tier()

    capability = Capability.model_validate_json(
        Path(args.capability).read_text(encoding="utf-8")
    )
    params = _parse_params(args.param)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.out_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"replay {capability.capability_id} v{capability.version}  "
          f"(max auto risk tier = {max_auto_tier.value})")

    step_log: list[dict] = []
    result: ReplayResult
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = context = None
            try:
                browser = pw.chromium.launch(headless=not args.headed)
                context = browser.new_context()
                page = context.new_page()
                _sign_on_if_needed(page, capability.target_app)
                result = replay_capability(
                    capability, params, page,
                    retry_wait=args.retry_wait, on_step=step_log.append,
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

    _print(result, run_dir)
    return 0 if result.status in ("success", "business_outcome") else 1


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
