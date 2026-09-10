# AUDIT

A self-audit against `project_requirements.txt`, section by section, done before submission —
so gaps are disclosed by us, not discovered by a reviewer. Legend: ✅ implemented & verified ·
🟡 implemented, partial or not verified live · ❌ not implemented.

## §3.1 Goal-driven agent loop

| Item | Status |
|---|---|
| Accept goal + target as input | ✅ |
| LLM observe → decide → act against a live UI | ✅ |
| Stopping condition: max steps | ✅ |
| Stopping condition: wall-clock timeout | ❌ not implemented — only per-operation Playwright waits exist, not a loop-level time budget |
| Stopping condition: dead-end detection | ❌ not a distinct condition — the loop stops on malformed-output or selector errors, not on "stuck/repeating/no progress" |
| Works with no clean DOM | 🟡 cleaned-HTML + synthesized labels + label/role/text locators (no test-ID dependency); no accessibility-tree or screenshot+coordinates path |

## §3.2 Structured artifact

| Item | Status |
|---|---|
| Ordered steps | ✅ |
| Locator identification | ✅ one strategy+value per step, carried over as executed |
| Robustness reasoning captured *in the artifact* | ❌ no fallback-locator chain or per-step reasoning field; the robustness strategy is real but lives in code/REPORT, not the reviewable JSON |
| Typed input parameters | ✅ |
| Typed outputs + shape | 🟡 scalar types only (str/int/float/bool), no structured/list output |
| Checkpoint / success condition | ✅ |
| Versioned & reviewable | ✅ |

## §3.3 Deterministic replay

| Item | Status |
|---|---|
| No LLM call on replay | ✅ |
| Stable targeting | ✅ |
| Checkpoint verification | ✅ |
| Declared outputs returned | ✅ |
| Business outcome vs. failure distinction | ✅ verified live (`restricted`, `not_found`) |
| Recoverable conditions handled internally | ✅ retry-once, never surfaced if it succeeds |
| Hard failure with debuggable detail | ✅ verified live (target app unreachable) |
| Richer snapshot on a per-step replay failure | 🟡 no DOM snapshot on a mid-replay step failure; CLI-level failures do get a full traceback file |
| Coverage of the brief's full exceptional-state list | 🟡 not-found + permission-denied covered; validation errors and unexpected dialogs / session expiry only caught generically, not deliberately |

## §3.4 Safety & guardrails

| Item | Status |
|---|---|
| Allowlist enforcement | 🟡 implemented and unit-tested; no live evidence of blocking a real off-app attempt |
| Risk-tier distinction, risky class handled conservatively | ✅ verified live |
| No secrets/PII persisted to artifacts or logs | ✅ |
| Redaction reaches the LLM request itself, not just logs | ✅ |
| Disclosed gap | replay's `result.json` writes input params unredacted (see REPORT §6/§7) |

## §3.5 Evidence / observability

| Item | Status |
|---|---|
| Structured log of what + why | ✅ |
| ≥1 richer signal on failure | ✅ discovery (DOM + screenshots every step/escalation); 🟡 replay (no per-step DOM snapshot on failure) |

## §3.6 Escalation & handoff

| Item | Status |
|---|---|
| Detect + route with context | ✅ |
| Same live session control transfer | ✅ (requires `--headed`; documented) |
| Resume / complete after handoff | ✅ verified live — 3 escalations in one run, resumed to a real completion |
| Records what the human did | ✅ |

## §3.7 Heterogeneity & multi-tenant (design-only, per brief)

| Sub-point | Status |
|---|---|
| Surface abstraction seam | ✅ addressed in REPORT §4 |
| Multi-tenant reuse + drift detection | ✅ addressed in REPORT §4 |

## §4 — the one non-optional item

✅ **Verified.** `/evidence/discovery/` contains a genuine LLM-driven run (model, real observe/decide/act,
timestamps, real completion reasoning) — not a description of one.

## §5 — the required working thread

✅ **Demonstrated in `/evidence/`:** goal → LLM run → capability artifact → replay (all three result
types) → guardrail + sensitive-data escalation with live handoff → resumed completion.

Caveat: replay evidence covers the *read* capability only; the *write* (sub-account) flow was
exercised live via discovery + escalation, but no capability was recorded/replayed from it.

## §6 — deliverables

| Deliverable | Status |
|---|---|
| `/README.md` — setup, keys/config, demo path | ✅ |
| `/REPORT.md` — 7 headings, exact wording/order | ✅ |
| `/evidence/` — artifact + discovery + replay logs, ≥1 exceptional-state replay | ✅ 3 of 4 replay runs are exceptional (restricted, not_found, unreachable-app failure) |
| Public git repository | ✅ |

## §9 — ground rules

✅ Verified via a real `git log` scan across all history (not just the working tree) — no API
key, no PII pattern, no `.env` ever committed.

## Known gaps not otherwise called out in REPORT.md §7

These were found during this audit and are disclosed here rather than left implicit:

- No wall-clock timeout or dead-end-detection stopping condition (§3.1) — only max-steps exists.
- The artifact schema doesn't carry per-step robustness reasoning or fallback locators (§3.2) —
  the strategy is real but isn't in the reviewable JSON itself.
- The business-outcome signature table (§3.3) is a hardcoded module constant today, not yet
  configurable per-capability as the long-term design intends.

**What we'd build next**, in priority order: a per-step reasoning/fallback-locator field in the
artifact schema (closes the biggest §3.2 gap), a real wall-clock timeout + basic dead-end
heuristic for the discovery loop, and replay evidence for the write (sub-account) flow to match
the read flow's coverage.
