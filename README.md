# interface-ai

A computer-use system with two phases operating on a web UI that has no API:

- **Discovery** — an LLM agent drives a real browser via Playwright toward a goal,
  and distills a successful run into a versioned **Capability artifact**.
- **Replay** — that artifact is executed **deterministically, no model in the loop**,
  against the same action surface, returning `success` / `business_outcome` /
  `failure`.

See [CLAUDE.md](CLAUDE.md) for the architecture, conventions, and package layout.
See [REPORT.md](REPORT.md) for the full design write-up (architecture, artifact
schema, determinism & error handling, heterogeneity, escalation, safety, cuts).

## Requirements

- Python 3.12+
- An Anthropic API key (needed for **discovery** runs only — replay never calls it)
- Playwright's Chromium browser

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                 # fill in as below
```

Edit `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...      # required for discovery; NOT read by replay
AGENT_MODEL=claude-sonnet-5
TARGET_APP_PORT=5001
TARGET_APP_BASE_URL=http://127.0.0.1:5001
TARGET_APP_USERNAME=clerk         # local demo credentials, fake data only
TARGET_APP_PASSWORD=vault
AGENT_MAX_AUTO_RISK_TIER=safe     # safe | confirm | blocked — see Safety in REPORT.md
```

## Target app (`/target_app`)

A local Flask app standing in for a legacy credit-union back-office UI —
server-rendered, form posts, no JSON API, deliberately hostile markup (nested
tables, no test IDs).

```bash
python -m target_app
```

Serves on **http://127.0.0.1:5001** by default. Port 5000 is commonly held by
macOS Control Center (AirPlay Receiver), so the default is 5001; override with
`TARGET_APP_PORT` (in `.env` or the environment):

```bash
TARGET_APP_PORT=8080 python -m target_app
```

- Sign on with the demo operator `clerk` / `vault` (or set
  `TARGET_APP_USERNAME` / `TARGET_APP_PASSWORD`).
- Flow: `/member` lookup → `/member/<id>` detail →
  `/member/<id>/sub-account` → confirmation.
- Try member `10001`, `10003`, `10006`. `10004` is restricted (business
  outcome, used in the replay demo below); `10002`'s detail page is
  deliberately slow (exercises replay's retry-on-transient-slowness path).
- `POST /debug/reset` restores the in-memory seed (undoes sub-accounts opened
  during a session). Local-only, no login required.

This system has two long-lived processes — the target app, and whatever
command you're running against it. Use two terminals: one running
`python -m target_app` throughout, the other for everything below.

## Demo path

The exact sequence: an LLM-driven discovery run → recording it as a reusable
capability → replaying that capability twice, deterministically, with no LLM
call — once to a normal success, once to a known business-exception outcome.

### 1. Discovery run (LLM-driven, live)

```bash
python run.py --goal "Look up member 10003 and read their current savings balance" --max-steps 10 --headed
```

Drop `--headed` to run headless. Note the printed run id
(`artifacts/runs/<run-id>/`) for the next step.

### 2. Record the run as a reusable capability

```bash
python -m agent.record --run artifacts/runs/<run-id> \
  --capability-id lookup-savings-balance --name "Look up savings balance" \
  --description "Looks up a member by number and reads their current savings balance." \
  --output-name savings_balance --output-strategy next_cell --output-target "Savings"
```

Writes `artifacts/capabilities/lookup-savings-balance-v0.1.0.json` — a typed,
versioned, human-reviewable artifact. Output extraction is a human-supplied,
reviewed argument, not auto-inferred — see REPORT.md §2 for why.

### 3. Replay — success case (no LLM call)

```bash
python -m replay --capability artifacts/capabilities/lookup-savings-balance-v0.1.0.json \
  --param member_number=10003 --headed
```

Expected: `status: success`, `output: savings_balance = 812.55`.

### 4. Replay — business-exception case (no LLM call)

```bash
python -m replay --capability artifacts/capabilities/lookup-savings-balance-v0.1.0.json \
  --param member_number=10004 --headed
```

Expected: `status: business_outcome`, `code: restricted`.

Both replay runs write full step logs + a result summary to
`evidence/runs/<run-id>/`.

### 5. Optional — a write flow with guardrails + human escalation

```bash
python run.py --goal "Open a new sub-account for member 10003 and reach the confirmation screen" --max-steps 10 --headed
```

This goal involves write actions. Each one pauses the run and asks for a
human decision in the terminal, using the same already-open browser session
— write actions are never auto-executed at the default
`AGENT_MAX_AUTO_RISK_TIER=safe`. At each `GUARDRAIL STOP`:

- `resume` — approve the agent's proposed action and let it execute, **or**,
  for a field requesting sensitive data (e.g. the SSN verification field),
  type the real value into the already-open browser yourself first, then
  `resume` — the agent never autofills sensitive data itself.
- `reject` — stop the run cleanly.

Run `POST /debug/reset` (or restart the target app) afterward if you want a
clean slate before repeating the demo.

## Tests

```bash
pytest                 # whole suite, from repo root
pytest tests/target_app/
```

The suite runs fully offline — Anthropic API calls and most Playwright
interactions are mocked/faked, so `pytest` needs no API key, no running
target app, and no network access.

## Evidence

`/evidence/` contains saved logs and result summaries from real discovery and
replay runs, including one replay that hits the restricted-member business
outcome and one discovery run that exercises the full guardrail +
sensitive-data escalation + human handoff path.
