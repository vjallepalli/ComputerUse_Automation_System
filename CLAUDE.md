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
| Every discovery run (working/scratch artifacts) | `artifacts/runs/<run-id>.json` | ignored |
| Curated, redacted evidence bundle kept as a deliverable example | `evidence/examples/<name>/` | **tracked** |
| Curated Capability artifact(s) — the deliverable | `artifacts/<name>.json` (root) | **tracked** |

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

**Last updated:** 2026-09-08

### Built
- Repo skeleton: all package dirs with `__init__.py` placeholders, `/tests` mirror
  layout, `/artifacts/.gitkeep`.
- `.gitignore` (with `.env` excluded from the first commit), `.env.example`.
- This `CLAUDE.md`.
- Git repo initialized; initial commit made.

### Planned (nothing implemented yet)
- `/schema` — Pydantic models for Capability artifact, step, locator, result union.
- `/guardrail` — shared allowlist + risk-tier function.
- `/surface` — Playwright adapter implementing `observe()` / `act()`.
- `/evidence` — JSONL logger + redaction + snapshot writer.
- `/target_app` — Flask back-office UI stand-in.
- `/agent` — discovery loop.
- `/replay` — deterministic executor.
- `/escalation` — control-owner state machine + mock operator CLI.
- First tests: redaction, replay error taxonomy, guardrail blocking.
