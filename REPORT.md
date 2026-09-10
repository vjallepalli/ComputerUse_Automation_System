# REPORT

## 1. Architecture

The system is a Python + Playwright + Anthropic API stack, chosen for low setup friction and because Playwright's multiple locator strategies (role, label, text) are well suited to a UI with no test IDs. The target surface is an intentionally legacy-hostile Flask app (server-rendered, nested tables, no semantic markup) standing in for a real back-office banking screen.

The system has two independent execution paths that share a perception/action layer:

- **Discovery loop** (`agent/orchestrator.py`): observe → decide → guardrail-check → act → record, repeated until a `done` action or a stopping condition. "Decide" calls the Claude API with the current cleaned page state and a goal; every other step is deterministic code.
- **Replay engine** (`replay/engine.py`): given a recorded Capability artifact and input parameters, re-runs the same step sequence with **no LLM call** — substituting parameters, executing via the same action/executor layer, checking a success condition, and extracting typed outputs.

Both loops route every action through the same guardrail check before execution, and both use the same DOM-cleaning/perception code (`surface/dom.py`), so a fix or policy change (e.g. sensitive-data masking) can't silently diverge between the two paths.

**Key trade-off:** perception is text/DOM-based, not screenshot/vision-based. Given no test IDs and nested-table markup, a cleaned-HTML representation lets the model reason about concrete, locatable elements (label text, role, adjacent cells) that map directly to Playwright locators. Vision would need coordinate-based interaction, which is more brittle for this class of app and harder to make deterministic on replay. The cost is that purely visual cues (layout, color) are invisible to the agent — acceptable for this environment, and documented as a stretch item.

## 2. Artifact schema

A recorded discovery run is first a raw, disposable transcript (`artifacts/runs/<id>/steps.jsonl` + DOM/screenshot sidecars) — useful for debugging, not the reusable product. A separate **Capability** (`schema/capability.py`) is compiled from a successful run and is the actual deliverable an AI agent would invoke:

- `parameters`: typed inputs (e.g. `member_number: int`), auto-detected by matching a typed value against the goal text at record time — if a typed value also appears in the goal, it's treated as a per-invocation input; if not, it's a fixed literal (e.g. clicking "Retrieve").
- `steps`: ordered actions with their resolved locator strategy/value, and `value_template` supporting `{param}` substitution.
- `outputs`: typed, named values with an explicit extraction locator (strategy + target) — e.g. a `next_cell` strategy that reads the value adjacent to a labeled table cell.
- `success_condition`: a locator that must resolve on the final page for the run to count as successful.
- `version` / `created_from_run`: versioned and traceable back to the discovery run it came from, without embedding the raw transcript.

**Deliberate design choice:** output extraction targets are **not** auto-inferred from the model's freeform "done" reasoning — they are supplied explicitly by a human reviewer at record time (`--output-strategy`/`--output-target`). Guessing "which number is the answer" from natural language is unreliable; the brief asks for a reviewable artifact, so the recorder proposes structure and a human confirms what the capability actually returns. This was validated in practice: an early auto-derived extraction target was wrong (it returned the field label instead of the value), caught only by manually inspecting the artifact before trusting it.

## 3. Determinism & error handling

Replay never calls an LLM. It re-syncs the same structural label-synthesis used in discovery (adding `aria-label`s for unlabeled inputs, derived from adjacent cell text) before each step, so the same locator strategies resolve identically without any model reasoning involved.

The result contract (`schema/replay.py`) distinguishes three outcomes, matching the brief's taxonomy:

- **`success`** — success condition held, outputs extracted.
- **`business_outcome`** — a known, expected divergence (e.g. a restricted member), detected via a small configurable table of page-text signatures checked after each step, before extraction is attempted. Not a crash.
- **`failure`** — a hard stop with `step_number`, what was expected, and what was observed, for debugging.

Transient conditions (a selector that doesn't resolve, a timeout) are retried once after a short wait before being treated as a hard failure — covering the brief's "transient slowness" case without masking real problems behind infinite retries.

Both discovery and replay were validated against a real business-outcome case (a restricted member) and a real success case with correct output extraction, confirmed by inspecting the actual evidence, not just a green test suite.

## 4. Heterogeneity & multi-tenant

Only one concrete surface (a legacy server-rendered web app) is implemented, but the design has a seam intended to generalize:

- **Perception/action vs. recorded flow are separate.** `surface/dom.py` and `surface/executor.py` are the only code that knows about Playwright/DOM specifics. The Capability schema itself only knows about abstract locator strategies (`label`, `role_text`, `text_contains`, `next_cell`) and typed parameters/outputs — nothing DOM-specific leaks into the artifact. Extending to a desktop app or an accessibility-tree-based surface would mean writing a new `surface` implementation that satisfies the same `get_cleaned_dom` / `execute_on_page` contract, without touching the schema, recorder, or replay engine.
- **Multi-tenant reuse:** since a Capability's steps target elements by role/label/text rather than raw CSS/XPath, and many tenants run the same underlying vendor product with different branding, a capability recorded against one tenant's instance has a reasonable chance of resolving correctly against another tenant's instance of the same product, as long as labels/roles are stable even if visual styling differs. Where they diverge (a genuinely different field label), the artifact's locators would fail to resolve — which replay already surfaces as a clear `failure`, not a silent wrong action. A production version would version-pin a capability per vendor-product-version and detect drift by tracking a rising failure rate for a given capability against a given tenant, flagging it for re-recording rather than continuing to fail silently.
- **Not implemented / explicitly deferred:** per-tenant override/specialization of a shared capability, and any desktop or accessibility-tree surface implementation. Both are architecturally accommodated (the seam exists) but not built, given the time budget.

## 5. Escalation & handoff

Two related but distinct human-in-the-loop paths exist:

- **Risky-action escalation:** a lightweight keyword heuristic (`guardrail/policy.py`) classifies actions into `safe` / `confirm` / `blocked` tiers (e.g. a click on "Process" or "Submit" is `confirm`; a destructive-sounding action is unconditionally `blocked`). Anything above the configured `AGENT_MAX_AUTO_RISK_TIER` pauses the loop before `execute_on_page` is reached.
- **Sensitive-data escalation:** any `type` action targeting a field whose label matches a sensitive-data keyword list (SSN, tax ID, passphrase, PIN) is unconditionally `blocked`, regardless of configured tier, because the agent's proposed value for such a field cannot be trusted — see Safety below.

On a stop, the system writes a structured `EscalationRequest` (goal/capability, step, reason, tier, a DOM/screenshot reference) and blocks on terminal input, while the **same live browser session** (already open, not a fresh one) is left available for the human to act on directly. Two human intents are distinguished on resume:

- The human **approves the proposed action as-is** (browser untouched) → the agent executes it.
- The human **performs the step manually** in the browser (detected via URL and live field-value comparison) → the agent's proposed action is skipped, and the loop re-observes from the resulting state rather than replaying a now-stale decision.

For a sensitive field specifically, only the second path is permitted — a bare "approve" is refused and re-prompted, because the agent's proposed value is necessarily fabricated (it never saw the real value) and executing it would silently write wrong data.

This was validated with a real run: the agent correctly stopped before submitting a new sub-account, a human typed the real verification value directly into the browser, and the run resumed and completed correctly — with the human's action, not its content, recorded in the evidence log.

**Mocked, per the brief's scope note:** the "operator console" is the same terminal plus the already-open browser window, not a separate web UI. The pause/resume/reject control-transfer mechanism itself is real; only the interface a human uses to see/act on it is minimal.

## 6. Safety

- **Allowlist:** actions are restricted to the capability's/run's configured `target_app` origin; anything outside it is a hard block, independent of risk tier.
- **Risk tiers:** `safe` (auto-executed) / `confirm` (escalates above the configured ceiling) / `blocked` (never auto-executed regardless of configuration — a hard floor, not just a default).
- **Sensitive data is masked before it reaches the model, not only before logging.** This was a real finding during development: initial redaction only scrubbed step *logs*, but the underlying value was still being sent to the Claude API as part of ordinary page-reading context. The fix masks matching values (by label adjacency and by SSN-shape pattern) inside the DOM-cleaning step itself, so the model never receives the raw value in the first place — a stronger guarantee than log-time redaction alone. A related regression (masking an *empty* sensitive field, making it look pre-filled and causing the agent to skip it) was caught and fixed during testing.
- **Manual-intervention logging** was corrected to omit the value entirely (not merely redact it) for steps a human completed off-screen from the agent's perspective — since restating even a redaction marker implied the agent had "typed" something it never actually saw.

**Known limits:** the risk-tier classifier is a keyword heuristic, not a real classifier — it can both over- and under-flag actions whose text doesn't match the configured keyword lists. A production version should let risk tier be reviewed and set explicitly per `CapabilityStep` at record time, the same review philosophy already used for output extraction. Output-side redaction (whether a capability's declared outputs could themselves leak sensitive data) was not audited. Replay's result JSON currently writes input `params` unredacted — flagged, not fixed, given time constraints.

## 7. Cuts

Deliberately deferred, given the time budget:

- Parameterization heuristic is a simple substring match against the goal text — no support for parameters not literally present in the goal string.
- Only one target-app flow pair was exercised end-to-end for replay (read-only lookup) plus one live discovery-and-escalation run (a write flow); replay was not re-tested against the write flow's recorded artifact.
- Multi-tenant override/specialization and a second (desktop or accessibility-tree) surface implementation: architecturally accommodated, not built.
- Risk classification is keyword-based, not reviewed/authored per capability.
- Output-value redaction and replay's `params` redaction in result JSON.
- Member-number type inference (string → int) would silently lose leading zeros on real member IDs shaped that way — not handled.

**What we'd build next with more time:** a human-reviewed risk tier stored directly in the Capability schema (closing the biggest safety gap honestly disclosed above), a second surface implementation to prove the abstraction seam, and end-to-end replay evidence for the write (sub-account) flow to match the read flow's coverage.
