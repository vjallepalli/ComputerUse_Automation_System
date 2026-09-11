# Computer-Use Automation System

**An LLM figures out a legacy banking UI once. From then on, the job runs without it.**

This was built to navigate through a problem the brief poses directly: banks and credit unions run a long tail
of back-office software with no API — the only way in is the same UI a human operator uses. Paying
a model to re-read and re-reason about that UI on every single invocation is slow, expensive, and
non-deterministic in exactly the place production automation can't afford to be.

So the system works in two phases:

- **Discovery** — an LLM agent drives a real browser (via Playwright) toward a natural-language
  goal, reading the page, deciding an action, and acting — observe, decide, act, repeat. A
  successful run gets distilled into a typed, versioned **Capability artifact**: not a transcript,
  but a reusable contract (inputs, steps, outputs, a success condition).
- **Replay** — that artifact runs again with **zero model calls**. New parameters in, a structured
  result out: `success`, a known `business_outcome` (e.g. "member not found" — a real answer, not
  a crash), or a debuggable `failure`.

Two things I leaned on hard, because the target app is deliberately hostile (nested tables, no
test IDs, no semantic labels): every action is checked against a **safety guardrail** before it
runs — writes and sensitive-data fields are never auto-executed — and when the system genuinely
can't proceed safely, it **hands the same live browser session to a human**, waits, and resumes
once they're done. Both are demonstrated end-to-end with real evidence in `/evidence/`, not just
built and left untested — see the honest limits in [`SELF_CHECK.md`](SELF_CHECK.md) for what that
testing
actually surfaced.

The full thread the brief asks for — a goal, an LLM-driven run that completes it, a saved
capability artifact, a deterministic replay with typed inputs/outputs and error handling, a human
taking over the live session mid-run, and evidence from both a discovery and a replay run — is
demonstrated end-to-end in `/evidence/`, not just described below.

**[Design write-up](REPORT.md)** · **[Self-check against the brief](SELF_CHECK.md)** ·
**[Evidence from real runs](evidence/)** · **[Dev conventions](CLAUDE.md)**

## How it works

From an end user's side, there are really only two moments that matter: **the first time** you
ask for something new, and **every time after that**.

**The first time** — discovery:

```
"Look up member 10003's savings balance"
        │
        ▼
  observe the page → decide what to do (LLM, one action per turn) → act on the browser
        │
        │  repeats until the goal is met
        ▼
  record the run as a typed, reusable Capability artifact
```

**Every time after** — replay, no model involved:

```
capability.json + member_number=10002
        │
        ▼
  re-run the same recorded steps, exactly — zero LLM calls
        │
        ▼
  success / business_outcome / failure  (typed, structured result)
```

Two more things happen on either side of that, whenever they're needed — not on every run:

- **Before any risky or write action executes** (opening a sub-account, submitting a form,
  touching a sensitive field like an SSN), the system checks it against a guardrail. If it's
  outside what's allowed to run automatically, the run **pauses**, hands the same live browser
  window to a person, and **waits** — it never guesses at data it can't see, and it never
  silently proceeds past something it shouldn't.
- **If replay hits something it doesn't recognize** — an unreachable app, a page that never
  reaches its expected end state — it reports a clear, structured failure (what step, what was
  expected, what was actually seen) instead of crashing or returning a wrong answer silently.

That's the whole shape: reason once, act carefully, replay cheaply — and know when to stop and
ask a person instead of guessing.

## Requirements

- Python 3.12+
- An Anthropic API key (needed for **discovery** runs only — replay never calls it).
  Get one at [console.anthropic.com](https://console.anthropic.com) if you don't have one.
- Playwright's Chromium browser

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                 # fill in as below
```

Edit `.env`:

```
ANTHROPIC_API_KEY=sk-ant-... # required for discovery; NOT read by replay
AGENT_MODEL=claude-sonnet-5
TARGET_APP_PORT=5001
TARGET_APP_BASE_URL=http://127.0.0.1:5001
TARGET_APP_USERNAME=clerk # local demo credentials, fake data only
TARGET_APP_PASSWORD=vault
AGENT_MAX_AUTO_RISK_TIER=safe # safe | confirm | blocked — see Safety in 
REPORT.md
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
`TARGET_APP_PORT` — either set it in `.env` (keep `TARGET_APP_BASE_URL` in sync)
or pass it inline for one run:

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
`python -m target_app` throughout, the other for everything below. **Each
terminal needs its own `source .venv/bin/activate`** — a new terminal doesn't
inherit it from the first one.

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
  `resume` — the agent never autofills sensitive data itself. For member
  10003 specifically, the real value (visible on their account detail page,
  `http://127.0.0.1:5001/member/10003`) is `912-18-2247`.
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

## Project structure

```
agent/        discovery loop, LLM decision-making (agent/decide.py), run recording (agent/record.py)
surface/      Playwright DOM cleaning, label synthesis, sensitive-data masking, action execution
guardrail/    risk classification (safe/confirm/blocked) and allowlist enforcement
escalation/   human handoff — pause, live-session control transfer, resume/reject
replay/       deterministic replay engine + CLI (no LLM calls, ever)
schema/       Pydantic models — Capability, ReplayResult, GuardrailDecision, EscalationRequest
target_app/   the legacy-hostile Flask app used as the demo surface (server-rendered, no test IDs)
evidence/     curated logs from real discovery + replay runs (see above)
tests/        mirrors the package layout above
```

`agent/orchestrator.py` (discovery) and `replay/engine.py` (replay) both call into
`guardrail/` before any action executes and both use `surface/` for perception and
action — see [`REPORT.md`](REPORT.md) §1 for why that sharing matters.

## Background & references

For anyone unfamiliar with why this problem exists in the first place — most
back-office banking software genuinely has no API to integrate against, which is
the whole premise this system is built around:

- [Backbase — "Banking AI transformation: Why legacy systems can't be bolted
  on"](https://www.backbase.com/blog/banking-legacy-systems) — on why legacy core
  banking systems are monolithic, batch-oriented, and resistant to the kind of
  API-first integration modern tooling assumes.
- [Baseella — "What are Legacy Core Banking Systems?"](https://baseella.com/kb/what-are-legacy-core-banking-systems/) —
  a plainer breakdown of common legacy banking architectures (mainframe,
  on-premises, custom-built) and why "lack of API connectivity" specifically is
  one of their defining, recurring traits.

I found it useful, while building this, to relate the "record once, replay many"
idea to consumer tools that do something structurally similar — autofill tools
like Jobright, which drive an unfamiliar web form the way a human would rather
than integrating against a site's API. The difference here is the record/replay
split: this system runs the model once per unique task, then reuses a
deterministic, typed artifact for every invocation after that, instead of
re-reasoning with an LLM on every single run.
