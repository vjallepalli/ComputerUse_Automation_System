# CLAUDE.md

Orientation for every session in this repo. Read the rules block first, every time.

---

## Non-negotiable rules (restate before making changes)

1. **Redact before persist, always.** Raw PII, credentials, secrets, full account
   numbers, and API keys must never be written into `/evidence`, `/artifacts`, or any
   log line. Redaction is applied *before* data touches disk or a log sink — not as a
   later cleanup pass. If a code path can persist text, it goes through the redactor.

2. **Secrets live in `.env`**, which is gitignored from the very first commit
   (`.gitignore` was committed with the skeleton). Never hardcode a secret, never
   commit `.env`. `.env.example` is the checked-in template with placeholder values.

3. **The guardrail is one shared dependency.** `/agent` and `/replay` both import and
   call the *same* allowlist + risk-tier logic from `/guardrail`. That logic is never
   copy-pasted or re-implemented in either caller. If replay and discovery ever
   disagree about whether an action is allowed, that is a bug in how they call the
   shared module, not a reason to fork it.

4. **Every capability of the system ships with at least one test.** A change is not
   "done" until a test covers it. First three targets, in priority order:
   redaction (rule 1), the replay error taxonomy (`success` / `business_outcome` /
   `hard_failure`), and guardrail blocking (a `blocked` action must refuse in both
   discovery and replay).

---

## Project summary

This is a computer-use system with two phases operating on a web UI that has no API.

In the **discovery run**, an LLM agent is given a goal and drives a real browser
through Playwright. It loops: **observe** the page (accessibility tree + screenshot),
**decide** the next action from the goal and current state, **act** through the
Surface adapter, then **record** what it did. Every candidate action is checked
against the shared guardrail (`safe` / `confirm` / `blocked`) before execution. A
successful discovery run is distilled into a versioned **Capability artifact** — a
JSON document listing each step (action, target locators with fallbacks and the
agent's reasoning, input binding, wait condition, on-error policy) plus the resolved
risk tier per action.

In the **replay run**, that artifact is executed **deterministically with no model in
the loop**, against the same `act()` surface the agent used: locators are resolved
(primary, then fallbacks), inputs are bound from supplied parameters, the guardrail
is re-checked, and each step's outcome is asserted against what was recorded. Replay
returns a 3-way discriminated result: `success(outputs)` when the recorded flow
completed; `business_outcome(type, detail)` when the UI worked correctly but reported
a domain result the caller must handle (e.g. "account frozen", "duplicate request");
`hard_failure(step, expected, observed, evidence_ref)` when a step could not be
executed as recorded (locator gone, unexpected page, guardrail block).

The **target app** (`/target_app`) is a local Flask application standing in for a
legacy bank back-office UI — server-rendered pages, form posts, no JSON API.

---

## Repo layout

Each code package has an `__init__.py` with a one-line docstring placeholder.

| Path           | Role |
|----------------|------|
| `/agent`       | Discovery orchestrator: the `observe() -> decide() -> act() -> record` loop. LLM in the loop. Goal-driven. |
| `/replay`      | Deterministic replay executor. Consumes a Capability artifact, drives the same `act()` surface, **no LLM**. |
| `/surface`     | Playwright adapter. Perception (a11y tree + screenshot) and actions. The single interface both `/agent` and `/replay` use to touch the browser. |
| `/schema`      | Pydantic models: Capability artifact, steps, locators, results, the replay result union. Single source of truth for artifact shape. |
| `/guardrail`   | Allowlist + risk-tier check. Shared dependency imported by **both** `/agent` and `/replay`. Not duplicated (see rule 3). |
| `/escalation`  | Control-owner state machine (who holds control: `agent` or `human`) plus a mock operator CLI for answering `confirm`-tier prompts. |
| `/evidence`    | Structured JSONL logger, screenshot / DOM-snapshot writer, redaction. Everything persisted about a run goes through here. |
| `/target_app`  | Local Flask demo app: the legacy bank back-office UI stand-in. |
| `/artifacts`   | Saved Capability JSON files — the discovery run's output. Data only, not a Python package (`.gitkeep`, contents generated). |

### Output path convention (tracked vs. ignored)

Symmetric split so `git add -A` is always safe and curated deliverables never need `git add -f`:

| Written by | Path | Git |
|------------|------|-----|
| Every discovery/replay run (raw logs, screenshots, DOM snapshots) | `evidence/runs/<run-id>/` | ignored |
| Every discovery run (`steps.jsonl` + `dom/` sidecars + `meta.json`) | `artifacts/runs/<run-id>/` | ignored |
| Curated, redacted evidence bundle kept as a deliverable example | `evidence/examples/<name>/` | **tracked** |
| Recorded Capability artifact(s) — the deliverable | `artifacts/capabilities/<capability_id>-v<version>.json` | **tracked** |

Code that persists run output writes under `runs/` by default. Promoting a run to a
committed example is a deliberate copy into `examples/` (evidence) or `artifacts/`
root — after re-checking redaction.
| `/tests`       | Test suite, top-level, mirroring the package layout (`tests/agent/`, `tests/guardrail/`, …). |

Root files: `CLAUDE.md`, `.gitignore` (committed first), `.env.example`,
`requirements.txt` (added with first implementation), `README.md` (added later).

---

## Fixed names & conventions

Keep these identical across every file. They are the contract between the phases.

### Surface interface (`/surface`)
```
observe() -> State
act(action: Action) -> ActionResult
```
`State` carries the a11y tree + screenshot reference + current URL. `ActionResult`
carries what happened (success / error, post-action state reference). `/agent` and
`/replay` both consume *only* this interface — no direct Playwright calls elsewhere.

### Agent loop steps (`/agent`)
Literally named, in this order, once per iteration:
```
observe -> decide -> act -> record
```
- `observe` — call `Surface.observe()`.
- `decide` — LLM chooses the next `Action` from goal + `State`.
- `act` — guardrail check, then `Surface.act(action)`.
- `record` — append the step (and evidence) toward the Capability artifact.

### Replay result (`/schema`, produced by `/replay`)
3-way discriminated union — exactly one of:
```
success(outputs)
business_outcome(type, detail)
hard_failure(step, expected, observed, evidence_ref)
```
- `success` — recorded flow completed; `outputs` are the extracted return values.
- `business_outcome` — UI behaved correctly but returned a domain result the caller
  must branch on (not an error). `type` is a stable enum-ish tag, `detail` is context.
- `hard_failure` — a step could not run as recorded. `step` = index/id, `expected`
  vs `observed`, `evidence_ref` points into `/evidence` for the captured snapshot.

### Risk tiers (`/guardrail`)
Every action resolves to exactly one:
```
safe      -- execute without asking
confirm   -- requires control-owner approval via /escalation before executing
blocked   -- never execute; refuse in both discovery and replay
```

### Capability artifact step shape (`/schema`)
Every step in an artifact has all of:
```
action                              # the Surface action kind + params
target:
  primary_locator                   # first locator strategy to try
  fallback_locators: [...]          # ordered alternates if primary fails
  reasoning                         # why the agent picked this target (discovery-time note)
input_binding                       # where the step's input value comes from (param name / constant / none)
wait_for                            # condition that must hold before the step is considered complete
on_error                            # policy: retry / fallback / escalate / hard_fail
```
The artifact itself is **versioned** (schema version + capability version) so replay
can refuse an artifact it does not know how to run.

---

## Environment & workflow

- Python, standard-library `venv` + `requirements.txt` + `pip` (no Poetry/uv).
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install -r requirements.txt` (added with first implementation code)
  - `playwright install chromium`
- Config via `.env` (copy from `.env.example`). Only the discovery agent reads LLM
  keys; replay must run with no LLM key present.
- Tests: `pytest` from repo root.
- LLM: default to the latest capable Claude model (`claude-sonnet-5`) for the agent.

---

## Current status

> Keep this section updated at the end of each working session so the next session
> knows what is actually built vs. still planned. Ask the user to confirm updates.

**Last updated:** 2026-09-09 (mask sensitive data before it reaches the model; never autofill secret fields)

### Built
- Repo skeleton: all package dirs with `__init__.py` placeholders, `/tests` mirror
  layout, `/artifacts/.gitkeep`.
- `.gitignore` (with `.env` excluded from the first commit), `.env.example`.
- This `CLAUDE.md`.
- Git repo initialized; initial commit made.
- `requirements.txt` — Flask + pytest (grows as later packages land).
- `/target_app` — Flask back-office stand-in. App factory `create_app()` in
  `target_app/app.py`, seed data in `target_app/data.py` (6 fake members,
  in-memory). Thin session login (`/login`, creds from `.env`, default
  `clerk`/`vault`). Flow: `/member` lookup → `/member/<id>` detail →
  `/member/<id>/sub-account` form → confirmation. Legacy-hostile markup:
  nested `<table>` layout, no `id`/`data-testid`, `<span onclick>` +
  `<input type=button>` submits, generic field names (`q`, `f1`–`f3`).
  Deterministic failures: unknown id → 404; bad sub-account input → 400
  re-rendered same page; member `10004` restricted → 403; member `10002`
  detail page sleeps 2s (wait/timeout hook).
  New-account numbers use an explicit per-member counter (`sub_seq` on the
  member dict) → reproducible `SUB-<id>-01` on the first opening after a reset.
  `POST /debug/reset` (ungated, login-exempt, local-only) restores the pristine
  seed via `data.reset()` against a `_PRISTINE` deep copy.
  Runs on `127.0.0.1:5001` by default (5000 is taken by macOS Control Center);
  override with `TARGET_APP_PORT`. Run with `python -m target_app` (entrypoint
  in `target_app/__main__.py`); needs no flags.
- `tests/target_app/test_failure_states.py` — asserts not-found, validation
  error, permission denied (plus a happy-path contrast). `pytest` green.
- `README.md` — setup + run instructions (target app on :5001, tests).
- `/schema/action.py` — the model's per-step action shape (not the Capability
  artifact yet). `Target{strategy,value}` + `TypeAction`/`ClickAction`/
  `DoneAction` discriminated on `action`; `parse_action(dict)` is the
  deterministic gate. `ACTION_TOOL` is the Anthropic tool def (one forced tool,
  loose top-level schema, parse_action enforces per-variant rules).
- `/surface/dom.py` — `get_cleaned_dom(page) -> CleanedDom(html, synthetic_labels)`
  + pure `clean_html(str) -> str` (both route through one `_clean`). Strips
  script/style/head/comments/presentational attrs/event handlers, unwraps
  non-semantic wrappers, prunes empty spacer cells, keeps tables + text + every
  interactive element. ~60–75% smaller on target_app pages. **Synthesises
  `aria-label`** on fields labelled only by an adjacent `<td>` (nearest preceding
  cell text; dedup suffix `(1)`/`(2)`; real label/`for`/`aria-label` never
  touched; no adjacent text → left alone). `_synthesize_labels` is the single
  source of truth — it mutates the tree *and* returns `{selector, match_index,
  label}` pairs. `apply_synthetic_labels(page, pairs)` replays those onto the
  live DOM via `page.evaluate` (missing selectors warned, not raised). Per-turn
  order: `get_cleaned_dom` → `apply_synthetic_labels` → model → `execute_on_page`.
  **Live value overlay:** `page.content()` serialises the stale HTML `value`
  *attribute*, not the DOM `value` *property* — after `.fill()` a field's
  attribute still reads empty. `get_cleaned_dom` reads every field's live value
  in one `page.evaluate` and writes it onto the tree before serialising, so the
  model never sees a field it just filled as blank and retype it forever.
  `clean_html` (no page) skips this.
  **Sensitive masking** (runs in `_clean`, so `clean_html` *and* `get_cleaned_dom`,
  discovery + replay): a real SSN / Tax ID must never reach the model, displayed
  or typed-in — **but only mask a NON-EMPTY value**. An empty sensitive field
  stays visibly `value=""` (masking an empty field makes the model think it's
  filled and skip the human hand-off). Two passes, `guardrail.policy`'s ONE
  shared keyword list + `SSN_SHAPE_RE`: (1) a cell whose **immediate** preceding
  sibling cell is a sensitive label → its display text, or a non-empty field
  value it holds (incl. a live value just overlaid), becomes `[MASKED]` (the
  static `NNN-NN-NNNN` format-hint cell, two past the label, is deliberately
  left — no real data); (2) regex sweep → `NNN-NN-NNNN` digits anywhere become
  `[MASKED-SSN]`. Masking changes ONLY what the model sees; `execute_on_page`
  still types the real value. `read_field_values(page)` is shared with handoff.
- `/surface/executor.py` — `execute_on_page(page, action)`: strategy → Playwright
  (`get_by_label` / `get_by_role` / `get_by_text`), exactly-one-match or
  `SelectorResolutionError`, then fill/click; `done` is a no-op report.
- `/agent/decide.py` — `ask_claude(goal, dom, history, *, client=None) -> Action`,
  the loop's `decide` step. Anthropic `messages.create` with `SYSTEM_PROMPT`
  (goal comes per-call not hardcoded; `done` only when the DOM already shows the
  asked-for result; the 3 strategies are the only ones; reason from the DOM
  text only), a user message of goal + cleaned DOM + one-line-per-step history
  (no DOM snapshots), forced `tool_choice` on `next_action`, thinking disabled.
  Model from `AGENT_MODEL` env (fallback `claude-sonnet-5`). Tool input →
  `parse_action`; a missing/malformed tool call or `ValidationError` becomes
  `AgentActionError` (typed, catchable) — no retry here, that's the loop's call.
- `anthropic==1.4.0` added to `requirements.txt`.
- `/agent/orchestrator.py` — the discovery loop + CLI. `python run.py --goal
  "..."` (root shim) or `python -m agent`. `main()` loads `.env` (python-dotenv),
  hard-checks `ANTHROPIC_API_KEY` (exit 2, clear message — no first-call
  traceback), computes `artifacts/runs/<UTC-ts>/`, calls `run_discovery` (launches
  chromium, `goto` start URL, programmatic `_sign_on_if_needed` convenience, then
  `run_loop`). `run_loop(goal, page, logger, *, max_steps=25, client=None) ->
  (exit_code, reason)`: per step `get_cleaned_dom` → `apply_synthetic_labels` →
  `ask_claude` → **record before act** → `execute_on_page` (skipped for `done`).
  Stops on DoneAction (0), max steps (1), `AgentActionError` / `SelectorResolution
  Error` (1, recorded as a step, summarised to stderr — never swallowed, never a
  bare traceback). `StepLogger` writes `steps.jsonl` (one JSON object per step;
  DOM offloaded to `dom/step-NNN.html` + sha256/chars/path; `decision` =
  `Action.model_dump()`; `outcome`/`error`) plus `meta.json` (goal/model/start_url
  /result). Console: one `[n] verb … -> status` line per step.
- `python-dotenv==1.0.1` added to `requirements.txt`.
- `/schema/capability.py` — the **Capability artifact** models: `Capability`
  (`schema_version` + semver `version`, `capability_id` slug, `created_from_run`
  reference, `target_app`, `parameters`/`steps`/`outputs`/`success_condition`),
  `Parameter` (name/type/description/example), `CapabilityStep` (step_number,
  action, reused `schema.action.Target`, `value_template` = literal or
  `{param}`), `Extraction`/`OutputSpec` (read strategies: the 3 action ones +
  `text_of` + `next_cell` = text of the `<td>` after the cell whose exact text
  is `target`), `SuccessCondition`. Every field documented; cross-checks that
  `{param}` refs are declared. Not the raw transcript — a reviewable, invokable
  capability.
- `/agent/record.py` — `record_capability(run_dir, *, output_name,
  output_strategy, output_target, capability_id, name, description) -> Capability`
  + `write_capability`. Reads `meta.json`/`steps.jsonl`; refuses `exit_code != 0`
  (`RecorderError`). Parameterisation heuristic: a `type` step whose text is
  verbatim in `meta.goal` becomes a `Parameter` named from the field label
  (`"Member number"` → `member_number`), step gets `value_template="{member_number}"`;
  text absent from the goal stays literal. Output targeting is an explicit
  reviewed arg (not inferred from the model's `done` reason). `success_condition`
  defaults to the output's own selector. Step targets carried over verbatim.
  CLI: `python -m agent.record --run … --output-strategy … --output-target …`
  → `artifacts/capabilities/<id>-v<version>.json`.
- `/schema/replay.py` — `ReplayResult` (3-way: `success` / `business_outcome` /
  `failure`; exactly one of `outputs`/`business_outcome`/`failure` set),
  `BusinessOutcome` (code/message — a legitimate result), `FailureDetail`
  (step_number/expected/observed/message; 0 = params, -1 = engine bug). No
  `recoverable` status — transient step errors are retried once internally.
- `/replay/engine.py` — `replay_capability(capability, params, page, *,
  retry_wait=0.5, on_step=None) -> ReplayResult`, **no LLM**. Validates params vs
  declared (mismatch → failure step 0, before `goto`); `goto(target_app)`;
  per step: `get_cleaned_dom` + `apply_synthetic_labels` (structural, no API) →
  substitute `{param}` → build `TypeAction`/`ClickAction` from `step.target` →
  `execute_on_page`, **retry once** after `retry_wait` on
  `SelectorResolutionError`/Playwright timeout; after each step check
  `KNOWN_BUSINESS_OUTCOMES` (page-text markers — seeded `access restricted`
  → `restricted`, `no such member` → `not_found`; global for the demo, belongs
  per-capability in prod) → `business_outcome`, no extraction. Then
  `success_condition` must resolve (else failure) → extract each `OutputSpec`
  (reuses `surface.executor.resolve_locator`; `text_contains`/`text_of`/
  `next_cell` → `inner_text`, coerced to `OutputSpec.type`) → `success`.
  Absolute backstop: always returns a `ReplayResult`.
  TODO markers for guardrail re-check (rule 3) and evidence redaction (rule 1).
- `/replay/cli.py` + `__main__.py` — `python -m replay --capability … --param
  name=value [--headed]`. Prints the result; writes `result.json` +
  `steps.jsonl` to `evidence/runs/<run-id>/` (gitignored). Exit 0 for
  success/business_outcome, 1 for failure/params mismatch. An **outer**
  try/except over browser launch + sign-on + the engine call turns any
  CLI-setup exception (target app unreachable, chromium missing, sign-on error)
  into a `failure` `ReplayResult` (`step_number -1`) — same evidence + print +
  exit path as any failure; the traceback goes to `evidence/runs/<id>/error.txt`,
  never the terminal. The engine's own internal backstop is unchanged.
- `surface/executor.py` — extracted `resolve_locator(page, strategy, value)`
  (shared by executor + replay). Read-only strategies: `text_of` (locate as
  `text_contains`), `next_cell` (exact-match a `<td>` via `get_by_role("cell",
  exact=True)`, return `following-sibling::td[1]`; raises
  `SelectorResolutionError` on 0 / >1 / no-next-sibling).
- `/schema/guardrail.py` — `RiskTier` (str-Enum, **ordered by risk** not string:
  safe < confirm < blocked, comparison operators overridden), `GuardrailDecision`
  (`allowed_automatically` / `tier` / `reason` / `allowlist_violation`).
- `/guardrail/policy.py` — the shared guardrail (CLAUDE.md rule 3), imported by
  BOTH loops. `classify_risk(action)` — keyword heuristic (documented as MVP,
  not a classifier; prod = per-capability human-reviewed annotation): `click`
  text containing delete/remove → `blocked`; submit/process/confirm/create/
  "open sub-account" → `confirm`; **`type` into a field whose label matches
  `SENSITIVE_FIELD_KEYWORDS` (password/passphrase/ssn/tax id/pin) → `blocked`,
  unconditionally** (the agent must never autofill a secret — its value is a
  guess built from masked context); everything else → `safe`.
  `check_allowed(url, base)` — scheme+host(+path-prefix) match; off-app is a
  categorical hard block. `evaluate(...) -> GuardrailDecision`: allowlist
  violation always wins; else `allowed = tier != blocked and tier <= max_auto`.
  `resolve_max_auto_tier()` reads `AGENT_MAX_AUTO_RISK_TIER` (default/garbage →
  safe). ONE shared `SENSITIVE_FIELD_KEYWORDS` + `SSN_SHAPE_RE` used by
  `redact_type_value` (log → `[REDACTED]`), `classify_risk` (above), and
  `surface.dom` masking (model DOM → `[MASKED]`/`[MASKED-SSN]`).
- `/schema/escalation.py` — `EscalationRequest` (references a DOM sidecar by
  path), `HandoffResult` (`resume`/`reject` + `intervened`/before-after).
- `/escalation/handoff.py` — `request_escalation(page, decision, ctx, *,
  input_fn=input)`: writes the request + cleaned-DOM snapshot + screenshot under
  `escalation/requests/<run-id>/`, prints what/why + that the SAME open browser
  is the human's now, then **blocks on `input()`** for `resume`/`reject`. On
  resume flags whether the page changed — url, cleaned-DOM, **or any raw live
  field value** (the last matters because the DOM masks the sensitive field:
  the human typing the SSN wouldn't otherwise be visible). MOCKED vs REAL:
  real pause/control-transfer/resume in one session; the "operator console" is
  this terminal + that browser window (no separate web UI — out of scope).
  **`ctx.requires_human_value`** (set for a guardrail-blocked sensitive `type`):
  "approve as-is" is disallowed — a bare `resume` re-prints why and waits; it
  proceeds only once `intervened` is true (the human actually typed a value).
  `reject`/EOF/exhausted-input always exit — bounded, never a silent loop.
- **Wired into both loops before `execute_on_page`**: `agent/orchestrator.run_loop`
  and `replay/engine._replay` call `guardrail.evaluate` per action; not
  auto-allowed → `request_escalation`. **reject** → run stops cleanly (discovery
  exit 1 / replay `failure` "human rejected escalation at step N"). **resume**
  branches on `HandoffResult.intervened` (url or cleaned-DOM changed while the
  human had control): `intervened=True` → human did it by hand → **skip**
  `execute_on_page`, re-observe/re-decide (discovery) or skip the step and let
  `success_condition` validate (replay); `intervened=False` → human **approved**
  the proposed action → **execute the original action**, logged distinctly
  (`outcome.note` "approved by human; executed as proposed" vs "human completed
  the step manually; skipped"). For a sensitive `type` the "approve" path does
  not exist — the caller only ever sees `reject` or `resume`+`intervened=True`
  from handoff, so a guessed secret can never reach `execute_on_page` (asserted
  in both loops). (Conflating resume's two meanings made an approved write never
  run and re-escalate forever.) The guardrail decision is logged on **every**
  action (auto-allowed included). **A typed value never lands raw anywhere the
  model/reviewer reads it**: `StepLogger.log` *and* `_console_line` *and* replay's
  `on_step` all run a `type` value through `redact_type_value` — auto-executed,
  human-approved-executed, or skipped. For a **manual-intervention** step
  (`resume`+`intervened=True`) the value is *omitted entirely*, not just
  redacted — the agent never typed it (a human did, off to the side, unseen by
  the agent), so `decision.text` becomes
  `[not executed by agent; human entered a value directly]` and the console line
  describes the hand-off rather than restating the (fabricated) proposed value.
  `decision.target` is kept — *what* the agent proposed is the auditable fact.
  `run_loop`/`replay_capability` take `target_app` + `max_auto_tier`; both CLIs
  print the resolved tier; replay adds `--max-auto-tier`. `meta.json` gains
  `target_app` + `max_auto_risk_tier`. (Known gap: `replay/cli.py` still writes
  `ReplayResult.params` raw to `result.json` — param-level redaction is TODO.)
- Tests: `tests/schema/`, `tests/surface/`, `tests/agent/`, `tests/replay/`,
  `tests/guardrail/test_policy.py` (classify incl. sensitive-`type`→blocked /
  allowlist / evaluate / redact / env), `tests/guardrail/test_wiring.py` (risky
  action escalates not executes in both loops; resume skip vs approve-execute;
  reject stops cleanly; off-app allowlist block; sensitive `type` → blocked,
  redacted in log, never executed), `tests/escalation/test_handoff.py` (input()
  mocked: request file + DOM sidecar; resume/reject/intervened/EOF; sensitive:
  bare resume re-prompts and is bounded, resume-after-value proceeds),
  `tests/surface/test_dom.py` (Tax ID / SSN-shape / overlaid-value masking).
  `pytest` green (161). **No real API/replay run yet.**
- **Finding (pre-loop sanity check):** raw markup has no real `<label>`, so the
  `label` strategy is dead against the raw live page and `role_text` is ambiguous
  for the >1 unnamed inputs on the sub-account form. Resolved by the aria-label
  synthesis + `apply_synthetic_labels` push — after the push, `label` resolves
  `f1/f2/f3` one-to-one on the live page. `role_text` still works for links /
  `<input type=submit|button>`; `text_contains` for the `<span>` submits.

### Planned (not implemented yet)
- `/surface` — `observe()` / `act()` wrappers over dom.py + executor.py;
  a11y tree + screenshot capture (currently the loops call dom.py / executor.py
  directly).
- `/evidence` — dedicated JSONL logger + robust redaction + snapshot writer
  (today: `StepLogger` in `agent/orchestrator.py`, a keyword redactor in
  `guardrail/policy.py`, ad-hoc snapshots in `escalation/handoff.py`).
- Risk tier as a per-`CapabilityStep` field, human-confirmed at record time
  (extends `agent/record.py`'s reviewed-output-extraction pattern) — replaces the
  replay-time keyword inference.
- `/escalation` — a persistent control-owner state machine (who holds control)
  beyond the per-event pause/resume that exists now.
