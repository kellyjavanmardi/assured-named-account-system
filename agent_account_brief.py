"""
Assured — Account-Brief Generator (Part 1 agent)

A system, not a one-off chat: retrieve -> data-quality gate -> assemble
prompt -> call Claude -> validate grounding -> deliver -> log.

Triggered on demand, one account at a time, by the "Generate Brief"
button on the dashboard — run_account_brief() below is exactly what that
click calls. Not a batch job.

build_prompt() below is the function Part 2 opens the hood on: it is the
exact dynamic-prompt-assembly logic, parameterized on account/contacts/
signals/constraint, that this script calls in step 5 of run_account_brief().
In the live room, the account/contacts/signals become {{variables}} in
Claude Console; the constraint becomes the panel's curveball.

In production the "retrieve" step reads three warehouse tables (fed
nightly by 6sense, Clay, influ2, and HockeyStack syncs). Here it reads the
same JSON snapshot the dashboard renders from, so both surfaces are
provably looking at one source of truth.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Literal, Optional

import anthropic

DATA_PATH = Path(__file__).parent / "data" / "assured_data.json"
MODEL = "claude-opus-5"
REPORT_DATE = date(2026, 7, 31)
SIGNAL_LOOKBACK_DAYS = 45

Constraint = Optional[Literal["champion_arming", "save_play", "expansion_push"]]


# ---------------------------------------------------------------------------
# 1. Retrieve — stand-in for the warehouse read
# ---------------------------------------------------------------------------

_CACHE: dict | None = None


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(DATA_PATH.read_text())
    return _CACHE


def get_account(name: str) -> dict:
    data = _load()
    for row in data["accounts"]:
        if row["Account"] == name:
            return row
    raise ValueError(f"no account named {name!r} in Named Accounts")


def get_top_contacts(account: str, n: int = 3) -> list[dict]:
    data = _load()
    contacts = [c for c in data["contacts"] if c["Account"] == account]
    seniority_rank = {"C-Suite": 0, "SVP": 1, "VP": 2, "Director": 3}
    contacts.sort(
        key=lambda c: (seniority_rank.get(c["Seniority"], 9), -c["Engagement (0-100)"])
    )
    return contacts[:n]


def get_recent_signals(account: str, lookback_days: int = SIGNAL_LOOKBACK_DAYS) -> list[dict]:
    data = _load()
    out = []
    for s in data["signals"]:
        if s["Account"] != account:
            continue
        age = (REPORT_DATE - datetime.strptime(s["Date"], "%Y-%m-%d").date()).days
        if age <= lookback_days:
            out.append(s)
    strength_rank = {"High": 0, "Medium": 1, "Low": 2}
    out.sort(key=lambda s: (strength_rank.get(s["Strength"], 9), s["Date"]), reverse=False)
    out.sort(key=lambda s: s["Date"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# 2. Data-quality gate — decide before spending a model call
# ---------------------------------------------------------------------------

@dataclass
class QualityGateResult:
    proceed: bool
    flags: list[str]
    fallback_reason: str | None = None


def caio_risk_flag(account: str, contacts: list[dict], signals: list[dict]) -> dict | None:
    """Same check the dashboard runs: a Job Change signal naming a new CAIO
    that postdates the matching contact's last known activity (or has no
    matching contact at all) means the roster is stale for this persona."""
    job_change = next(
        (s for s in signals if s["Signal Type"] == "Job Change"
         and re.search(r"Chief AI Officer|CAIO", s["Detail"], re.I)),
        None,
    )
    if not job_change:
        return None
    caio_contact = next(
        (c for c in contacts if re.search(r"Chief AI Officer", c.get("Persona", ""), re.I)),
        None,
    )
    stale = caio_contact is None or caio_contact["Last Activity"] < job_change["Date"]
    if not stale:
        return None
    return {"signal": job_change, "contact": caio_contact}


def run_quality_gate(account: dict, contacts: list[dict], signals: list[dict]) -> QualityGateResult:
    flags = []
    days_since_touch = account["Days Since Touch"]

    if not signals and not contacts:
        return QualityGateResult(
            proceed=False,
            flags=["no_contacts", "no_recent_signals"],
            fallback_reason="No engaged contacts and no signal activity in the lookback "
                             "window — insufficient data for a grounded brief. Routing to "
                             "human review instead of guessing.",
        )

    if days_since_touch > 60:
        flags.append(f"dormant_{days_since_touch}d")

    if not signals:
        flags.append("no_recent_signals")

    if caio_risk_flag(account["Account"], contacts, signals):
        flags.append("caio_roster_stale")

    # Verified against the full dataset: 5 of 30 accounts have a "Pilot
    # Milestone" signal (product-usage telemetry) while their CRM Pilot
    # Status says "None" — the product and CRM systems have drifted apart
    # for these accounts. Citing that signal as-is would tell an AE their
    # account has a pilot running when the CRM says it doesn't.
    if account["Pilot Status"] == "None" and any(
        s["Signal Type"] == "Pilot Milestone" for s in signals
    ):
        flags.append("pilot_signal_status_mismatch")

    return QualityGateResult(proceed=True, flags=flags)


# ---------------------------------------------------------------------------
# 3. Output contract — enforced via structured outputs, not prompt instruction
# ---------------------------------------------------------------------------

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "account": {"type": "string"},
        "situation_summary": {
            "type": "string",
            "description": "1-2 sentences: where this account is and why it matters this week.",
        },
        "key_signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "type": {
                        "type": "string",
                        "description": "The bare Signal Type only, exactly as listed (e.g. 'Job Change') — never include the source or strength here.",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Copied character-for-character from that signal's Detail text above — no additions, no appended commentary, no source or strength. Put your own analysis in risk_or_opportunity or next_best_action instead.",
                    },
                },
                "required": ["date", "type", "detail"],
                "additionalProperties": False,
            },
            "description": "Only signals that were actually provided in the input — do not invent one.",
        },
        "risk_or_opportunity": {
            "type": "string",
            "description": "2-3 sentences, scannable in seconds. Name whichever the data actually supports — a real risk, a real opportunity, or both if the account genuinely has each. Don't force a risk where there isn't one, and don't skip a real risk just because there's also good news.",
        },
        "next_best_action": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "1-2 sentences: the specific move to make, concrete enough to act on immediately.",
                },
                "rationale": {
                    "type": "string",
                    "description": "1 sentence: why this is the right move, grounded in the data above.",
                },
            },
            "required": ["action", "rationale"],
            "additionalProperties": False,
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "data_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Anything the brief could NOT verify — stale roster, no signal in window, etc.",
        },
    },
    "required": [
        "account", "situation_summary", "key_signals",
        "risk_or_opportunity", "next_best_action", "confidence", "data_gaps",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 4. build_prompt — THE function Part 2 opens the hood on
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the account-brief generator for Assured's named-account \
marketing system. You write tight, pre-meeting briefs for account owners (AEs) at \
a P&C-carrier-focused AI company selling FNOL (First Notice of Loss) automation.

Ground every claim in the data you are given. Never invent a signal, a date, a \
contact, or a number that was not provided to you. If the data is thin, say so in \
data_gaps rather than filling the gap with something plausible-sounding.

Carrier-compliance guardrails: you are writing pre-sales marketing content, not a \
claims, legal, or regulatory document. Never make coverage, liability, or \
claims-outcome determinations. Never promise a specific ROI figure, timeline, or \
compliance/regulatory outcome that isn't explicitly present in the input data — \
recommend, don't commit. Never reference another carrier's data, deals, or contacts \
when writing about this account; each brief is scoped to one carrier only.

Write for a busy AE who has 90 seconds before the call. Be specific and actionable, \
not generic sales-speak."""


def build_prompt(
    account: dict,
    contacts: list[dict],
    signals: list[dict],
    quality_flags: list[str],
    constraint: Constraint = None,
) -> str:
    """Assemble the user-turn prompt from a dashboard row's worth of data.

    This is the dynamic prompt: swap `account`/`contacts`/`signals` for any
    row and the same template produces a grounded brief for that account.
    `constraint` is the lever for the panel's live curveballs — it does not
    change what data is injected, only what the model is asked to do with it.
    """
    contact_lines = "\n".join(
        f"- {c['Contact']} ({c['Title']}), {c['Buying Role']}, "
        f"engagement {c['Engagement (0-100)']}/100, last activity: "
        f"{c['Last Activity']} ({c['Last Activity Type']})"
        for c in contacts
    ) or "No engaged contacts on file for this account."

    signal_lines = "\n".join(
        f"- {s['Date']} · {s['Signal Type']}: {s['Detail']} (source: {s['Source']}, strength: {s['Strength']})"
        for s in signals
    ) or "No signals logged in the last 45 days."

    flag_lines = "\n".join(f"- {f}" for f in quality_flags) or "None."

    constraint_block = {
        None: "",
        "champion_arming": (
            "\n\nCONSTRAINT: This account's Chief AI Officer is skeptical and leaning "
            "toward building this in-house on Anthropic's models directly. Do not write "
            "a generic brief. Write next_best_action as build-vs-buy objection prep: arm "
            "the champion (not the CAIO) with 2-3 concrete reasons buying beats building "
            "for THIS account specifically, grounded in what you know about their stage "
            "and stack from the data above."
        ),
        "save_play": (
            "\n\nCONSTRAINT: Treat this account's intent as newly Cooling, even if the "
            "data below shows otherwise — assume the pilot has gone quiet. Write "
            "next_best_action as a save play: a specific, low-friction re-engagement "
            "move for this account's actual champion, not a generic check-in."
        ),
        "expansion_push": (
            "\n\nCONSTRAINT: This account is in production and healthy. Write "
            "next_best_action as an expansion push — the next specific line of "
            "business, workflow, or team this account could extend Assured into, "
            "grounded in what you know about their segment and stage."
        ),
    }[constraint]

    return f"""ACCOUNT
Name: {account['Account']}
Tier: {account['Tier']} | Segment: {account['Segment']} | Owner: {account['Account Owner']}
Intent: {account['Intent (0-100)']}/100, trend {account['Intent Trend']}
Buying stage: {account['Buying Stage']} | Deal stage: {account['Deal Stage']}
Pilot status: {account['Pilot Status']} | Open pipeline: ${account['Open Pipeline ($)']:,.0f}
Days since last touch: {account['Days Since Touch']}
Momentum note (from CRM): {account['Momentum Signal']}

TOP CONTACTS
{contact_lines}

RECENT SIGNALS (last {SIGNAL_LOOKBACK_DAYS} days)
{signal_lines}

DATA-QUALITY FLAGS (raised by the retrieval layer before you saw this — factor these in)
{flag_lines}
{constraint_block}

Produce the account brief now, following the required output schema exactly. \
Every entry in key_signals is a strict citation, not a place for your own analysis: \
"type" is the Signal Type word only (e.g. "Job Change"), and "detail" is the exact \
text after the colon in RECENT SIGNALS, character-for-character — no source, no \
strength, no appended commentary like "— likely..." or "— suggests...". Do not \
summarize two signals into one or add a signal that isn't listed. Put everything \
you want to say about why a signal matters in risk_or_opportunity or \
next_best_action instead — that's what those fields are for."""


# ---------------------------------------------------------------------------
# 5. Call Claude with the output contract enforced
# ---------------------------------------------------------------------------

def call_claude(user_prompt: str) -> dict:
    # Thinking is on by default on claude-opus-5 and shares max_tokens with the
    # output — for a bounded structured-extraction task like this (not open-ended
    # reasoning), disabling it keeps the token budget predictable and avoids the
    # JSON getting truncated mid-generation. Explicit, not an oversight.
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=3072,
        thinking={"type": "disabled"},
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
        messages=[{"role": "user", "content": user_prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined this request: {response.stop_details}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------------------
# 6. Grounding validation — the anti-hallucination gate
# ---------------------------------------------------------------------------

def validate_grounding(brief: dict, signals: list[dict]) -> list[str]:
    """Every cited signal must trace back to one actually passed in — date,
    type, AND detail. Checking only (date, type) would let a real date paired
    with a fabricated detail slip through as "grounded." Empty list means the
    brief is clean to deliver."""
    problems = []
    known = {(s["Date"], s["Signal Type"], s["Detail"]) for s in signals}
    for cited in brief.get("key_signals", []):
        key = (cited.get("date"), cited.get("type"), cited.get("detail"))
        if key not in known:
            problems.append(f"cited signal not in input: {cited}")
    return problems


# ---------------------------------------------------------------------------
# 7. Orchestration — retrieve -> gate -> assemble -> call -> validate -> log
# ---------------------------------------------------------------------------

def run_account_brief(account_name: str, constraint: Constraint = None) -> dict:
    account = get_account(account_name)
    contacts = get_top_contacts(account_name)
    signals = get_recent_signals(account_name)

    gate = run_quality_gate(account, contacts, signals)
    if not gate.proceed:
        return {"status": "insufficient_data", "reason": gate.fallback_reason}

    prompt = build_prompt(account, contacts, signals, gate.flags, constraint)
    brief = call_claude(prompt)

    problems = validate_grounding(brief, signals)
    if problems:
        # in production: one retry with a stricter reminder, then route to
        # the human review queue on a second failure. never deliver ungrounded.
        return {"status": "grounding_failed", "problems": problems, "raw": brief}

    # log(account_name, prompt, brief, model=MODEL, prompt_version="v1")
    # deliver_to_slack(account["Account Owner"], brief)
    return {"status": "ok", "brief": brief}


if __name__ == "__main__":
    result = run_account_brief("Progressive", constraint="champion_arming")
    print(json.dumps(result, indent=2))
