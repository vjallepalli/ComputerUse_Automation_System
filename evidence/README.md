# Evidence

Deliverable #3: a saved capability artifact plus logs from a discovery run and a
replay run (with replay runs covering the exceptional states, not just success).

All runs here are **real runs from live testing** — an actual LLM-driven
discovery loop and actual deterministic replays against the local target app.
Copies only; the originals are untouched under `artifacts/runs/`,
`evidence/runs/`, and `escalation/requests/`.

| Folder | What it shows | Brief |
|---|---|---|
| `capability/lookup-savings-balance-v0.1.0.json` | The recorded capability: typed `parameters` (`member_number: int`), ordered `steps` with `{param}` binding, a typed `outputs` spec (`savings_balance: float`, extracted via `next_cell "Savings"`), and a `success_condition`. Versioned (`schema_version` + semver). Human-readable. | 3.2 |
| `discovery/lookup/` | Discovery run for *"Look up member 10003 and read their current savings balance"*. Clean 3-step LLM loop (`type` → `click` → `done`), no guardrail stop. `meta.json` (goal, model, exit 0), `steps.jsonl` (per-step `decision` + `outcome`), `dom/step-00N.html` (the cleaned DOM the model saw each turn). This is the run `capability/…json` was recorded from (`created_from_run`). | 3.1 |
| `discovery/sub-account-with-escalation/` | Discovery run for *"Open a new sub-account for member 10003 and reach the confirmation screen"* — a write flow. 8 steps, **exit 0, real `done` at the "Sub-Account Opened" confirmation screen** (`SUB-10003-02`). Three guardrail stops, all handled: step 3 `click "Open sub-account"` (`confirm`, human-approved), step 6 `type` into *Verify member SSN* (**`blocked`** — sensitive field; human entered the real value in the browser, `intervened=true`), step 7 `click "Process"` (`confirm`, human-approved). Step 6's `decision.text` is `"[not executed by agent; human entered a value directly]"` — never a raw or fabricated SSN. `escalation-requests/` holds the three persisted `EscalationRequest` JSONs + DOM snapshots + screenshots for those stops. | 3.1, 3.4, 3.6 |
| `replay/success/` | Replay `member_number=10003` → `status: success`, `outputs.savings_balance = 812.55`. Deterministic, no LLM. `steps.jsonl` shows each step `ok`, `success_condition` `ok`, extraction `ok`. | 3.3 |
| `replay/business_outcome_restricted/` | Replay `member_number=10004` → `status: business_outcome`, `code: restricted`. The UI worked correctly and returned a domain result; replay stops and reports it as a legitimate outcome, not a failure, and does not attempt extraction. | 3.3 |
| `replay/business_outcome_not_found/` | Replay `member_number=99999` → `status: business_outcome`, `code: not_found`. Same taxonomy, different domain outcome. `steps.jsonl` also shows the per-step `guardrail` decision recorded (`safe`/auto). | 3.3, 3.4 |
| `replay/failure/` | Replay when the target app was **unreachable** → `status: failure`, `failure.step_number: -1`, `observed` = `net::ERR_CONNECTION_REFUSED`. `steps.jsonl` has a `{"stage": "cli-setup", "status": "failed"}` record; the full traceback is in `error.txt`, **not dumped to the console**; the CLI's defined failure exit code (1) is used. | 3.3 |

## Notes

- **`discovery/lookup/dom/step-003.html`** — this run predates the
  `get_cleaned_dom` sensitive-data masking. Member 10003's Tax ID
  (`NNN-NN-NNNN`-shaped) appeared once in that DOM sidecar; it has been replaced
  with `[MASKED]` for this deliverable (brief rule 1: no raw PII in
  `/evidence`), and that step's `dom.sha256` / `chars` in `steps.jsonl` updated
  to match, with a `note`. The unmodified run is at
  `artifacts/runs/20260909T194536Z/`. Every other DOM/snapshot here was captured
  with masking in place — no SSN-shaped value survives a scan of this folder.
- Replay runs write no DOM sidecars (that is discovery's `StepLogger`); their
  directories are `result.json` + `steps.jsonl` (+ `error.txt` for the failure).
- The `sub-account-with-escalation` screenshots are taken at the moment each
  guardrail stop is raised — before the human acts — so the SSN field is empty
  in them; the `*.after.dom.html` snapshot (post-human-input) shows the field
  as `value="[MASKED]"`.
