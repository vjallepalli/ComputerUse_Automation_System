# Adversarial testing — live evidence

Captured during a self-directed adversarial pass (Part B: live/integration
scenarios against the **real** local target app on `127.0.0.1:5001` and the
**real** `python -m replay` engine — no mocks).

This folder is **separate from the curated `/evidence/` deliverable** and does
not touch it. Nothing here contains raw PII: replays use member *numbers*
(not PII by the app's design); a scan for SSN-shaped strings, member names, and
credentials over this folder is clean.

Discovery-side scenarios that need the Anthropic API (an LLM in the loop) were
**not run live** — this session has no key and must not call the API. They are
covered by mocked-but-equivalent persisted tests
(`tests/agent/test_orchestrator_edge.py`) instead; see B3 below.

| Dir | Scenario | Result |
|---|---|---|
| `B1a_allowlist_deadport/` | Capability `target_app` points at a dead port (`:5999`); real app is on `:5001`. `python -m replay`. | Clean `failure`, `failure.step_number = -1`, `observed = net::ERR_CONNECTION_REFUSED`, traceback → `error.txt` (not console), exit 1. `execute_on_page` never reached. |
| `B1b_allowlist_violation/` | Capability `target_app` pinned to a path prefix the app won't stay under (`:5001/vault-only`). | The engine re-navigates the page to `target_app` before each guardrail check, so `check_allowed` stays self-consistent (`allowlist_violation: false` in `steps.jsonl`) — the guardrail *is* consulted every step, then the type step fails cleanly because the page is a 404. **Finding:** see note 1. |
| `B2_slow_member_10002/` | Replay `member_number=10002` — the deliberately slow (2 s) detail page. | Both steps `attempts: 1` — **retry did NOT engage**. A 2 s delay is well inside Playwright's auto-wait; the retry path is only for `SelectorResolutionError` / `PlaywrightTimeoutError` (>30 s). Replay then `failure`s at `success_condition` because member 10002 has no Savings account — correct. Wall time ≈ 3.7 s. |
| `B3` (no dir) | Discovery with `--max-steps 1` against a multi-step flow / an impossible goal. | Not run live (needs the API). `tests/agent/test_orchestrator_edge.py` covers it: loop runs exactly the budget, exits `EXIT_INCOMPLETE` with `reached max steps (N)`, `meta.json` marks the run unrecordable, and the recorder refuses that transcript. No hang/crash. |
| `B4_broken_capability/` | Hand-broken artifacts to `python -m replay`: (a) missing `steps`/`description`, `version` as an int; (b) valid JSON but `steps: []` and `version: "1.0"`. | One clear stderr line each — `invalid capability artifact '...': N validation error(s) -- ...` — exit 1, **no traceback**. (Was a raw stack trace before the fix.) |
| `B5_missing_file/` | `--capability /no/such/capability.json`. | `cannot read capability file '...': [Errno 2] No such file or directory`, exit 1, no traceback. (Was a raw `FileNotFoundError` trace before the fix.) |
| `B6_param_type_mismatch/` | `--param member_number=not_a_number` (declared `int`). | `failure` at `step_number 0` — *"parameter 'member_number' is not a valid int"* — before any page interaction. `steps.jsonl` has `{"stage": "params", "status": "failed"}`. Clean, not a confusing downstream error. |
| `B7_runid_collision/` | Two `python -m replay` runs back-to-back with no `--run-id`. | Two **distinct** run dirs (`replays/…`). `collision_demo.txt` shows the old bare-second stamp collapsing 5 rapid ids into 1 shared dir vs. the new `default_run_id()` giving 5 distinct ones. |

## Notes

1. **`allowlist_violation` in replay is effectively unreachable against this
   target app.** The engine navigates the page to `capability.target_app`
   itself before every guardrail check, so a mis-pointed-but-*reachable*
   `target_app` stays self-consistent (no violation); a mis-pointed
   *unreachable* one fails earlier as a clean CLI setup error (B1a); and the app
   has **no cross-origin links**, so no recorded step can drift off-origin at
   replay time. The shared allowlist logic itself is well covered by unit tests
   (`tests/guardrail/test_policy_edge.py` adds 8 look-alike-host cases), and the
   engine→guardrail→escalation wiring for a violation is exercised end-to-end
   with the **real** guardrail module in
   `tests/replay/test_engine_edge.py::test_offorigin_page_triggers_a_real_allowlist_violation_and_no_execute`.
   Reported as a gap/judgment-call, not a bug: the re-check is meaningful only
   against mid-flow cross-origin drift, not a bad `target_app`.

2. All replay evidence here was produced with `TARGET_APP_*` at their
   `.env.example` defaults and `/debug/reset` called before each run.
