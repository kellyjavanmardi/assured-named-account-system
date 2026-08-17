# Assured — Named-Account System

**Part 1** of the AI & Data Marketing Engineer panel exercise: a weekly named-account dashboard and the agentic workflow that powers it, built over the provided synthetic dataset (30 accounts, 42 contacts, 55 signals).

**Live dashboard (real Claude calls):** PENDING_VERCEL_URL
*(two tabs — Weekly Dashboard, Agent Architecture. Click "Generate Brief" on any account — it's a real, live call to claude-opus-5, not a canned preview.)*

**Static fallback:** https://claude.ai/code/artifact/afe742d6-4ebf-4bff-9a12-1de8fbd7b017 — same two tabs; "Generate Brief" shows a clear error there instead of a live result, since a claude.ai Artifact page can't hold an API key (see **Live deploy**, below).

---

## What's here

| File | What it is |
|---|---|
| `dashboard.html` | The weekly dashboard — self-contained HTML/CSS/JS, real dataset embedded inline |
| `agent-architecture.html` | The agent's system-architecture doc, same design system as the dashboard |
| `assured-system.html` | Both of the above combined into one tabbed page — canonical source |
| `index.html` | Same content as `assured-system.html`, duplicated at the root so Vercel serves it at `/` with no rewrite needed |
| `api/agent_account_brief.py` | The agent itself — real, tested Python, not pseudocode. Lives inside `api/` so the deployed function *is* this exact file, not a copy |
| `api/data/assured_data.json` | The provided dataset, read by both this file and (via the embedded JSON) the dashboard |
| `api/generate_brief.py` | Vercel serverless function — the real backend behind the dashboard's button |
| `api/pyproject.toml` | The function's own dependency (`anthropic`) and Python version, scoped to `api/` as its own Vercel Service |
| `vercel.json` | Declares `api/` as a Vercel Service and routes only `/api/*` to it — everything else serves as plain static files |

Dashboard and agent read from the **same JSON snapshot**, so both surfaces are provably looking at one source of truth.

---

## The dashboard

A view the CEO and Head of Sales can read in under five minutes and immediately know where to focus this week and why — covering 30 named accounts, built around one question: *what deserves attention right now?*

- **Act Now This Week** — accounts with intent ≥ 60 and zero meetings booked, plus any account carrying a stale CAIO (Chief AI Officer) risk signal — a new AI decision-maker hired in, with no contact on record for that persona yet.
- **Protect & Expand** — accounts already in an Expansion deal stage.
- Full sortable/filterable account grid and a pilot-status mini table.
- A "Generate Brief" button on every Act Now, Protect & Expand, and Pilots-in-Flight account — a real, live call to the agent below, not a canned preview. Click it and it retrieves that account's real contacts and signals, runs the quality gate, and calls claude-opus-5 in front of you. See **Live deploy**, below, for how that's wired up safely.

Styling pulls real hex values from assured.com's brand palette, mapped consistently to meaning across the whole page (e.g. magenta = act-now/no-meeting, orange = CAIO-risk specifically, red = cooling/closed-lost only) — not decorative color, every color is a label.

![Dashboard](screenshots/dashboard.png)

## The agent

**Focus: an account-brief generator.** Given an account, it retrieves that account's contacts and recent signals, runs a data-quality gate, assembles a grounded prompt, calls Claude for a structured JSON brief, validates that every cited signal actually exists in the input, and delivers/logs the result.

Rather than building three separate agents for the brief's three next-best-action options, next-best-action adapts via a single `constraint` parameter — `champion_arming`, `save_play`, or `expansion_push` — that changes what the prompt asks for without changing the pipeline.

```
Trigger (dashboard button) → Retrieve → Quality Gate → Assemble Prompt → Call Claude → Validate Grounding → Deliver + Log
```

**Quality gate** — routes to human review instead of guessing when there's nothing to ground a brief in, and flags (rather than blocks) partial-data cases:
- No contacts and no recent signal activity → `insufficient_data`, does not call the model
- Dormant 60+ days → flagged, brief still generated
- No recent signals → flagged
- CAIO hired, contact roster stale → flagged
- **`pilot_signal_status_mismatch`** — a real data-quality issue found while building this: 7 of 30 accounts carry a "Pilot Milestone" product signal while their CRM `Pilot Status` field still says `None`. Flagged so the brief never implies a pilot that isn't on record.

**Guardrails** enforced in the system prompt — every cited signal must literally exist in the input (checked as exact `(date, type, detail)` set membership post-hoc, not fuzzy-matched); no coverage/liability/claims-outcome determinations; no unsupported ROI/timeline/compliance promises; never reference another carrier's data.

**Output** is a structured JSON brief (`output_config.format: json_schema`, all fields required, `additionalProperties: false`) — not free text — so it can be rendered, logged, or piped into CRM/Slack without a parsing layer.

**Trigger & observability** (designed in `agent-architecture.html`, not wired to live infra — see Assumptions). Deliberately one trigger, not a batch job: the dashboard's "Generate Brief" button *is* the front door — clicking it on any account is what calls `run_account_brief()` for that account, synchronously, in seconds.

| | |
|---|---|
| Trigger | Dashboard button click — one account, on demand |
| Delivery | Rendered straight back to the account owner in the dashboard |
| Eval | 5-account golden set, hand-graded, scored weekly by Claude-as-judge |
| Alerting | Grounding-check failure rate > 5% pages the on-call owner |
| Logging | Every run logs input, output, prompt version, latency, and token cost |

**\*Where this goes next (not built):** ideally this fires itself — the moment a meeting lands on the calendar or a CRM stage flips to "meeting set," the same pipeline runs automatically and the brief is waiting before the account owner asks for it. That needs Claude connected to the team's calendar/CRM to watch for the event, which is out of scope for this exercise — so today, the account owner triggers it with one click instead.

Run it (needs `ANTHROPIC_API_KEY` set):

```bash
pip install anthropic
python api/agent_account_brief.py
```

By default this generates a real brief for Progressive under the `champion_arming` constraint and prints the JSON. Swap the account name or constraint in the `if __name__ == "__main__"` block at the bottom of the file to try others.

![Agent architecture](screenshots/agent-architecture.png)

## Live deploy

The dashboard's button had to actually call Claude live, not fake it — but a static page (like a published claude.ai Artifact) has nowhere safe to hold an API key: anything in client-side JS is visible to anyone who opens dev tools, regardless of hosting. Plain static hosting doesn't fix that either.

The real fix: `api/generate_brief.py` is a Vercel serverless function. It holds `ANTHROPIC_API_KEY` as a server-side environment variable — never sent to the browser — and runs `run_account_brief()` from `api/agent_account_brief.py` for real, for whichever account you click (same file, not a copy — `api/` is fully self-contained as its own Vercel Service). The dashboard's button calls `GET /api/generate_brief?account=<name>`, same-origin, and renders whatever comes back in the agent's real output contract.

```
Browser click → fetch('/api/generate_brief?account=...')
             → Vercel Function (holds the API key)
             → run_account_brief() → claude-opus-5
             → JSON brief back to the browser
```

Deployed with GitHub → Vercel: push to `main`, Vercel builds and deploys both pieces from `vercel.json`'s [Services](https://vercel.com/docs/services) config — `api/` is declared as its own Vercel Service with its own `pyproject.toml`, and only `/api/*` is routed to it; everything else (`index.html`, the screenshots, etc.) is served as plain static files, untouched by the Python runtime. The only manual step is the `ANTHROPIC_API_KEY` environment variable, set in the Vercel project itself (not in this repo — `api/agent_account_brief.py` never hardcodes a key).

Only real accounts from the dataset are accepted — `get_account()` raises on anything else — so the account name can't be used to inject arbitrary text into the prompt.

## How this connects to Part 2

`build_prompt()` in `api/agent_account_brief.py` is the exact dynamic-prompt-assembly logic — parameterized on account, contacts, signals, and constraint — that Part 2 opens the hood on live: those become `{{variables}}` in Claude Console, and the constraint becomes the panel's curveball.

## Assumptions

- **Retrieval** is a static JSON snapshot standing in for three nightly-synced warehouse tables (6sense, Clay, influ2, HockeyStack) — same shape, no live warehouse connection assumed.
- **Trigger** is the dashboard's "Generate Brief" button, one account at a time — not a batch/cron job. This part *is* wired to live infra (see **Live deploy**); logging, alerting, and the golden-set eval are designed in the architecture doc, not built.
- **Model choice**: `claude-opus-5`, with extended thinking explicitly disabled (`thinking: {"type": "disabled"}`) — this is a bounded structured-generation task, not open-ended reasoning, and thinking otherwise shares the token budget with the JSON output.
- **"Recent" signal** = 45-day lookback window.
- **"Top contacts"** = ranked by seniority (C-Suite → Director) then by engagement, capped at 3.

---

*Dataset is the synthetic set provided for this exercise; no real Assured customer data is present.*
