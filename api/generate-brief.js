// Vercel serverless function backing the dashboard's "Generate Brief" button.
//
// A faithful port of the pipeline in ../agent_account_brief.py -- retrieve ->
// quality gate -> assemble prompt -> call Claude -> validate grounding ->
// respond. This file exists only because Vercel's Python runtime would not
// reliably surface Project Settings environment variables to a Python
// function in this project; the pipeline logic itself is unchanged from the
// tested Python reference. The Anthropic API key lives only here, as a
// Vercel environment variable -- it is never sent to the browser.
//
// GET /api/generate-brief?account=<name>&constraint=<optional>

const fs = require("fs");
const path = require("path");
const Anthropic = require("@anthropic-ai/sdk");

const DATA_PATH = path.join(__dirname, "..", "data", "assured_data.json");
const MODEL = "claude-opus-5";
const REPORT_DATE = Date.UTC(2026, 6, 31); // July is month 6 (0-indexed)
const SIGNAL_LOOKBACK_DAYS = 45;
const VALID_CONSTRAINTS = new Set(["champion_arming", "save_play", "expansion_push"]);

let _cache = null;
function load() {
  if (_cache === null) {
    _cache = JSON.parse(fs.readFileSync(DATA_PATH, "utf-8"));
  }
  return _cache;
}

function daysBetween(dateStr) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const t = Date.UTC(y, m - 1, d);
  return Math.round((REPORT_DATE - t) / 86400000);
}

function getAccount(name) {
  const data = load();
  const row = data.accounts.find((a) => a.Account === name);
  if (!row) throw Object.assign(new Error(`no account named '${name}' in Named Accounts`), { code: "NOT_FOUND" });
  return row;
}

function getTopContacts(account, n = 3) {
  const data = load();
  const seniorityRank = { "C-Suite": 0, SVP: 1, VP: 2, Director: 3 };
  return data.contacts
    .filter((c) => c.Account === account)
    .sort((a, b) => {
      const ra = seniorityRank[a.Seniority] ?? 9;
      const rb = seniorityRank[b.Seniority] ?? 9;
      if (ra !== rb) return ra - rb;
      return b["Engagement (0-100)"] - a["Engagement (0-100)"];
    })
    .slice(0, n);
}

function getRecentSignals(account, lookbackDays = SIGNAL_LOOKBACK_DAYS) {
  const data = load();
  const strengthRank = { High: 0, Medium: 1, Low: 2 };
  const out = data.signals.filter((s) => s.Account === account && daysBetween(s.Date) <= lookbackDays);
  // Two-pass sort mirrors the Python reference: stable sort by (strength, date)
  // then a stable re-sort by date descending -- net effect is date-desc with
  // strength as the tiebreaker among same-date signals.
  out.sort((a, b) => {
    const sa = strengthRank[a.Strength] ?? 9;
    const sb = strengthRank[b.Strength] ?? 9;
    if (sa !== sb) return sa - sb;
    return a.Date < b.Date ? -1 : a.Date > b.Date ? 1 : 0;
  });
  out.sort((a, b) => (a.Date < b.Date ? 1 : a.Date > b.Date ? -1 : 0));
  return out;
}

function caioRiskFlag(account, contacts, signals) {
  const jobChange = signals.find(
    (s) => s["Signal Type"] === "Job Change" && /Chief AI Officer|CAIO/i.test(s.Detail)
  );
  if (!jobChange) return null;
  const caioContact = contacts.find((c) => /Chief AI Officer/i.test(c.Persona || ""));
  const stale = !caioContact || caioContact["Last Activity"] < jobChange.Date;
  if (!stale) return null;
  return { signal: jobChange, contact: caioContact || null };
}

function runQualityGate(account, contacts, signals) {
  const flags = [];
  const daysSinceTouch = account["Days Since Touch"];

  if (signals.length === 0 && contacts.length === 0) {
    return {
      proceed: false,
      flags: ["no_contacts", "no_recent_signals"],
      fallbackReason:
        "No engaged contacts and no signal activity in the lookback window — insufficient data for a grounded brief. Routing to human review instead of guessing.",
    };
  }

  if (daysSinceTouch > 60) flags.push(`dormant_${daysSinceTouch}d`);
  if (signals.length === 0) flags.push("no_recent_signals");
  if (caioRiskFlag(account.Account, contacts, signals)) flags.push("caio_roster_stale");
  if (account["Pilot Status"] === "None" && signals.some((s) => s["Signal Type"] === "Pilot Milestone")) {
    flags.push("pilot_signal_status_mismatch");
  }

  return { proceed: true, flags };
}

const BRIEF_SCHEMA = {
  type: "object",
  properties: {
    account: { type: "string" },
    situation_summary: {
      type: "string",
      description: "1-2 sentences: where this account is and why it matters this week.",
    },
    key_signals: {
      type: "array",
      items: {
        type: "object",
        properties: {
          date: { type: "string" },
          type: {
            type: "string",
            description:
              "The bare Signal Type only, exactly as listed (e.g. 'Job Change') — never include the source or strength here.",
          },
          detail: {
            type: "string",
            description:
              "Copied character-for-character from that signal's Detail text above — no additions, no appended commentary, no source or strength. Put your own analysis in risk_or_opportunity or next_best_action instead.",
          },
        },
        required: ["date", "type", "detail"],
        additionalProperties: false,
      },
      description: "Only signals that were actually provided in the input — do not invent one.",
    },
    risk_or_opportunity: { type: "string" },
    next_best_action: {
      type: "object",
      properties: {
        action: { type: "string" },
        rationale: { type: "string" },
      },
      required: ["action", "rationale"],
      additionalProperties: false,
    },
    confidence: { type: "string", enum: ["high", "medium", "low"] },
    data_gaps: {
      type: "array",
      items: { type: "string" },
      description: "Anything the brief could NOT verify — stale roster, no signal in window, etc.",
    },
  },
  required: ["account", "situation_summary", "key_signals", "risk_or_opportunity", "next_best_action", "confidence", "data_gaps"],
  additionalProperties: false,
};

const SYSTEM_PROMPT = `You are the account-brief generator for Assured's named-account marketing system. You write tight, pre-meeting briefs for account owners (AEs) at a P&C-carrier-focused AI company selling FNOL (First Notice of Loss) automation.

Ground every claim in the data you are given. Never invent a signal, a date, a contact, or a number that was not provided to you. If the data is thin, say so in data_gaps rather than filling the gap with something plausible-sounding.

Carrier-compliance guardrails: you are writing pre-sales marketing content, not a claims, legal, or regulatory document. Never make coverage, liability, or claims-outcome determinations. Never promise a specific ROI figure, timeline, or compliance/regulatory outcome that isn't explicitly present in the input data — recommend, don't commit. Never reference another carrier's data, deals, or contacts when writing about this account; each brief is scoped to one carrier only.

Write for a busy AE who has 90 seconds before the call. Be specific and actionable, not generic sales-speak.`;

function buildPrompt(account, contacts, signals, qualityFlags, constraint) {
  const contactLines =
    contacts
      .map(
        (c) =>
          `- ${c.Contact} (${c.Title}), ${c["Buying Role"]}, engagement ${c["Engagement (0-100)"]}/100, last activity: ${c["Last Activity"]} (${c["Last Activity Type"]})`
      )
      .join("\n") || "No engaged contacts on file for this account.";

  const signalLines =
    signals
      .map((s) => `- ${s.Date} · ${s["Signal Type"]}: ${s.Detail} (source: ${s.Source}, strength: ${s.Strength})`)
      .join("\n") || `No signals logged in the last ${SIGNAL_LOOKBACK_DAYS} days.`;

  const flagLines = qualityFlags.map((f) => `- ${f}`).join("\n") || "None.";

  const constraintBlocks = {
    champion_arming:
      "\n\nCONSTRAINT: This account's Chief AI Officer is skeptical and leaning toward building this in-house on Anthropic's models directly. Do not write a generic brief. Write next_best_action as build-vs-buy objection prep: arm the champion (not the CAIO) with 2-3 concrete reasons buying beats building for THIS account specifically, grounded in what you know about their stage and stack from the data above.",
    save_play:
      "\n\nCONSTRAINT: Treat this account's intent as newly Cooling, even if the data below shows otherwise — assume the pilot has gone quiet. Write next_best_action as a save play: a specific, low-friction re-engagement move for this account's actual champion, not a generic check-in.",
    expansion_push:
      "\n\nCONSTRAINT: This account is in production and healthy. Write next_best_action as an expansion push — the next specific line of business, workflow, or team this account could extend Assured into, grounded in what you know about their segment and stage.",
  };
  const constraintBlock = constraint ? constraintBlocks[constraint] || "" : "";

  return `ACCOUNT
Name: ${account.Account}
Tier: ${account.Tier} | Segment: ${account.Segment} | Owner: ${account["Account Owner"]}
Intent: ${account["Intent (0-100)"]}/100, trend ${account["Intent Trend"]}
Buying stage: ${account["Buying Stage"]} | Deal stage: ${account["Deal Stage"]}
Pilot status: ${account["Pilot Status"]} | Open pipeline: $${Math.round(account["Open Pipeline ($)"]).toLocaleString("en-US")}
Days since last touch: ${account["Days Since Touch"]}
Momentum note (from CRM): ${account["Momentum Signal"]}

TOP CONTACTS
${contactLines}

RECENT SIGNALS (last ${SIGNAL_LOOKBACK_DAYS} days)
${signalLines}

DATA-QUALITY FLAGS (raised by the retrieval layer before you saw this — factor these in)
${flagLines}
${constraintBlock}

Produce the account brief now, following the required output schema exactly. Every entry in key_signals is a strict citation, not a place for your own analysis: "type" is the Signal Type word only (e.g. "Job Change"), and "detail" is the exact text after the colon in RECENT SIGNALS, character-for-character — no source, no strength, no appended commentary like "— likely..." or "— suggests...". Do not summarize two signals into one or add a signal that isn't listed. Put everything you want to say about why a signal matters in risk_or_opportunity or next_best_action instead — that's what those fields are for.`;
}

async function callClaude(userPrompt) {
  const client = new Anthropic();
  const response = await client.messages.create({
    model: MODEL,
    max_tokens: 3072,
    thinking: { type: "disabled" },
    system: SYSTEM_PROMPT,
    output_config: { format: { type: "json_schema", schema: BRIEF_SCHEMA } },
    messages: [{ role: "user", content: userPrompt }],
  });
  if (response.stop_reason === "refusal") {
    throw new Error(`Claude declined this request: ${JSON.stringify(response.stop_details)}`);
  }
  const textBlock = response.content.find((b) => b.type === "text");
  return JSON.parse(textBlock.text);
}

function validateGrounding(brief, signals) {
  const known = new Set(signals.map((s) => `${s.Date}|${s["Signal Type"]}|${s.Detail}`));
  const problems = [];
  for (const cited of brief.key_signals || []) {
    const key = `${cited.date}|${cited.type}|${cited.detail}`;
    if (!known.has(key)) problems.push(`cited signal not in input: ${JSON.stringify(cited)}`);
  }
  return problems;
}

async function runAccountBrief(accountName, constraint) {
  const account = getAccount(accountName);
  const contacts = getTopContacts(accountName);
  const signals = getRecentSignals(accountName);

  const gate = runQualityGate(account, contacts, signals);
  if (!gate.proceed) {
    return { status: "insufficient_data", reason: gate.fallbackReason };
  }

  const prompt = buildPrompt(account, contacts, signals, gate.flags, constraint);
  const brief = await callClaude(prompt);

  const problems = validateGrounding(brief, signals);
  if (problems.length) {
    return { status: "grounding_failed", problems, raw: brief };
  }

  return { status: "ok", brief };
}

module.exports = async (req, res) => {
  const account = (req.query.account || "").toString().trim();
  let constraint = req.query.constraint;
  if (Array.isArray(constraint)) constraint = constraint[0];
  if (!VALID_CONSTRAINTS.has(constraint)) constraint = undefined;

  if (!account) {
    res.status(400).json({ error: "missing required query param: account" });
    return;
  }

  try {
    const result = await runAccountBrief(account, constraint);
    res.status(200).json(result);
  } catch (err) {
    if (err.code === "NOT_FOUND") {
      res.status(404).json({ error: err.message });
      return;
    }
    res.status(502).json({ error: `agent call failed: ${err.message}` });
  }
};
