// Builds web/src/fixtures/call_easy_deal.json from a hand-written synthetic call.
// Usage (from web/): node scripts/gen-fixture.mjs src/fixtures/call_easy_deal.json
import { writeFileSync } from "node:fs";

const out = process.argv[2];
const frames = [];
let t = 0;
let auditId = 0;
const push = (dt, ev) => {
  t += dt;
  frames.push({ t, ev });
};

const BAL = 125000;
const MAX_BP = 5200; // PRIVATE
const FEE_ROW = 4500; // PRIVATE program fee per row
const BANK = 950; // PRIVATE
const curve = [];
for (let bp = 100; bp <= 10000; bp += 100) {
  const feasible = bp >= 2000 && bp <= MAX_BP && !(bp >= 3300 && bp <= 3500);
  curve.push({ bp, feasible });
}

function rows(n, total) {
  const pay = Math.floor(total / n);
  const out = [];
  for (let i = 0; i < n; i++) {
    const m = 11 + i; // Nov 2026 onward
    const y = 2026 + Math.floor((m - 1) / 12);
    const mm = ((m - 1) % 12) + 1;
    out.push({
      date: `${y}-${String(mm).padStart(2, "0")}-02`,
      creditor_payment_cents: i === n - 1 ? total - pay * (n - 1) : pay,
      program_fee_cents: FEE_ROW,
      bank_fee_cents: BANK,
      balance_cents: 68250 + i * 6550,
    });
  }
  return out;
}

const terms = {
  max_payments: { value: null, status: "UNKNOWN", evidence: [], history: [] },
  min_payment_cents: { value: null, status: "UNKNOWN", evidence: [], history: [] },
  payment_structure: { value: null, status: "UNKNOWN", evidence: [], history: [] },
  first_payment_date: { value: "2026-11-02", status: "ASSUMED", evidence: [], history: [] },
  max_segments: { value: 2, status: "ASSUMED", evidence: [], history: [] },
  max_token_pays: { value: null, status: "ASSUMED", evidence: [], history: [] },
  min_payment_tiers: { value: [], status: "ASSUMED", evidence: [], history: [] },
};
const beliefEv = () => ({
  type: "belief",
  terms: Object.entries(terms).map(([field, v]) => ({ field, ...structuredClone(v) })),
});

function audit(actor, event, payload, priv = false) {
  auditId += 1;
  const ts = new Date(Date.UTC(2026, 9, 7, 15, 0, 0) + t).toISOString();
  const row = { type: "audit", id: auditId, ts, actor, event, payload };
  if (priv) row.private = true;
  return row;
}

let phase = "OPENING";
let lastEval = null;

/**
 * One agent turn. `c` = creditor line (null for the opening).
 */
function turn(n, spec) {
  const {
    creditor,
    stance = null,
    ask_bp = null,
    ask_quote = null,
    extracted = [],
    dropped = [],
    intent,
    reason,
    reason_text,
    counter_bp = null,
    nlg,
    sentences,
    evalEv = null,
    blocked = [],
    nextPhase,
    timings,
    agreement = null,
  } = spec;

  if (creditor) {
    push(2600, {
      type: "transcript",
      role: "creditor",
      text: creditor,
      spoken: true,
      sentence_id: null,
      blocked: false,
    });
  }

  // Belief changes from verified terms.
  const changes = [];
  for (const term of extracted) {
    if (!term.verified) continue;
    const cur = terms[term.field];
    const newStatus = term.hedged ? "TENTATIVE" : "KNOWN";
    changes.push({
      field: term.field,
      old_value: cur.value,
      new_value: term.value,
      old_status: cur.status,
      new_status: newStatus,
      turn: n,
      quote: term.quote,
    });
    if (cur.value !== null) cur.history.push(cur.value);
    cur.value = term.value;
    cur.status = newStatus;
    cur.evidence.push({ turn: n, quote: term.quote });
    if (term.field === "max_payments" && terms.max_token_pays.status === "ASSUMED") {
      terms.max_token_pays.value = term.value;
    }
  }

  const serverMs = (timings.nlu_ms ?? 0) + (timings.engine_ms ?? 0) + (timings.policy_ms ?? 0) + (timings.nlg_ms ?? 0);
  // Agent reply lands after server processing.
  let dt = creditor ? Math.round(serverMs) + (timings.stt_ms ?? 0) : 400;
  const ids = sentences.map((_, i) => `t${n}-s${i}`);
  sentences.forEach((text, i) => {
    push(i === 0 ? dt : 40, { type: "say", id: ids[i], text });
    push(0, {
      type: "transcript",
      role: "agent",
      text,
      spoken: false,
      sentence_id: ids[i],
      blocked: blocked.length > 0,
    });
  });
  push(5, beliefEv());
  push(0, { type: "phase", phase, intent, turn: n });
  if (evalEv) lastEval = evalEv;
  if (evalEv) push(0, evalEv);
  for (const b of blocked) push(0, { type: "blocked", ...b });
  push(0, {
    type: "latency",
    turn: n,
    stt_ms: timings.stt_ms ?? null,
    nlu_ms: timings.nlu_ms ?? null,
    policy_ms: timings.policy_ms ?? null,
    nlg_ms: timings.nlg_ms ?? null,
    server_total_ms: Math.round(serverMs * 10) / 10,
  });

  // Audit tail for the turn.
  if (creditor) {
    push(0, audit("creditor", "utterance", { text: creditor, turn: n }));
    push(0, audit("llm", "llm_call", { role: "nlu", provider: "groq", model: "synthetic-nlu-model", latency_ms: timings.nlu_ms, prompt_tokens: 812, completion_tokens: 96, cache_hit: false, failover_from: null, error: null }));
    push(0, audit("nlu", "nlu_result", { stance, ask_bp, terms: extracted.filter((x) => x.verified).map((x) => x.field) }));
    for (const d of dropped) push(0, audit("nlu", "post_verify_drop", { field: d.field, reason: d.reason }));
    for (const c of changes) push(0, audit("belief", "belief_change", { field: c.field, old_status: c.old_status, new_status: c.new_status, quote: c.quote }));
  }
  push(0, audit("engine", "affordability", { max_bp: MAX_BP, feasible_points: curve.filter((p) => p.feasible).length }, true));
  if (evalEv) push(0, audit("engine", "evaluate", { feasible: evalEv.feasible, offer_total_cents: evalEv.offer_total_cents, program_fee_cents: evalEv.program_fee_cents }, true));
  push(0, audit("policy", "decide", { intent, reason }));
  if (nlg.mode === "llm") push(0, audit("llm", "llm_call", { role: "nlg", provider: "cerebras", model: "synthetic-nlg-model", latency_ms: timings.nlg_ms, prompt_tokens: 402, completion_tokens: 38, cache_hit: false, failover_from: null, error: null }));
  for (const b of blocked) push(0, audit("guard", "blocked", b));

  push(0, {
    type: "turn_trace",
    turn: n,
    creditor_text: creditor ?? null,
    stance,
    ask_bp,
    ask_quote,
    terms: extracted.filter((x) => x.verified).map(({ verified: _v, ...rest }) => ({ ...rest, verified: true })),
    dropped,
    belief_changes: changes,
    affordability: { max_bp: MAX_BP, curve },
    decide: { intent, reason, reason_text },
    counter_bp,
    nlg,
    spoken: sentences.map((text, i) => ({ id: ids[i], text })),
    timings,
  });
  push(0, { type: "turn_done" });

  // Client TTS finishes and acks each sentence; effects commit on the last ack.
  sentences.forEach((text, i) => {
    const speakMs = 260 + text.length * 55;
    const last = i === sentences.length - 1;
    if (last) phase = nextPhase;
    push(speakMs, { type: "phase", phase: last ? nextPhase : phase, intent: null, turn: n });
    if (lastEval) push(0, lastEval);
    if (last && agreement) {
      push(0, agreement);
      push(0, audit("orchestrator", "agreement_drafted", { bp: agreement.bp, offer_total: agreement.offer_total }));
    }
    push(0, { type: "turn_done" });
  });
}

const tmpl = (template, extra = {}) => ({
  mode: "template",
  template,
  guards: [
    { stage: "template", ok: true, reason: null },
    { stage: "rendered", ok: true, reason: null },
  ],
  fallback_used: false,
  ...extra,
});
const llm = (template, extra = {}) => ({ ...tmpl(template), mode: "llm", ...extra });

// ---- turn 0: opening
turn(0, {
  creditor: null,
  intent: "OPENING",
  reason: "opening",
  reason_text: "Open the call: say who we are and that an automated agent is speaking.",
  nlg: tmpl(
    "Hello, this is an automated agent calling on behalf of {firm_name} about a client's account with you. {opening_disclosure} What payment terms can you work with for a settlement?",
  ),
  sentences: [
    "Hello, this is an automated agent calling on behalf of Harbor Lane Debt Relief about a client's account with you.",
    "I am authorized to discuss settlement options for this account.",
    "What payment terms can you work with for a settlement?",
  ],
  nextPhase: "DISCOVERY",
  timings: { queue_ms: 0, engine_ms: 8.4, policy_ms: 0.1, nlg_ms: 0.3, tts_onset_ms: 290 },
});

// ---- turn 1: terms
turn(1, {
  creditor: "Sure. We can take up to eight monthly payments, at least one hundred dollars each, all the same amount.",
  stance: "info",
  extracted: [
    { field: "max_payments", value: 8, quote: "up to eight monthly payments", hedged: false, verified: true },
    { field: "min_payment_cents", value: 10000, quote: "at least one hundred dollars each", hedged: false, verified: true },
    { field: "payment_structure", value: "even", quote: "all the same amount", hedged: false, verified: true },
  ],
  dropped: [
    { field: "first_payment_date", value: "2026-11-01", reason: "quote_not_in_utterance", quote: "starting on the first" },
  ],
  intent: "ASK_SETTLEMENT",
  reason: "missing_ask",
  reason_text: "All required terms are known, so ask what settlement percentage they want.",
  nlg: tmpl("What settlement percentage of the balance are you looking for?"),
  sentences: ["What settlement percentage of the balance are you looking for?"],
  nextPhase: "NEGOTIATE",
  timings: { stt_ms: 410, queue_ms: 0, nlu_ms: 812, engine_ms: 9.1, policy_ms: 0.2, nlg_ms: 0.3, tts_onset_ms: 300 },
});

const evalAt = (bp, n, agreed = bp) => ({
  type: "eval",
  feasible: true,
  shape: "even",
  offer_total_cents: Math.round((BAL * bp) / 10000),
  program_fee_cents: FEE_ROW * n,
  assumed_fields: ["first_payment_date", "max_segments", "max_token_pays", "min_payment_tiers"],
  agreed_bp: agreed,
  rows: rows(n, Math.round((BAL * bp) / 10000)),
  additional_funds: null,
  max_bp: MAX_BP,
});

// ---- turn 2: ask 45%, counter 32%
turn(2, {
  creditor: "We are looking for forty-five percent of the balance.",
  stance: "offer",
  ask_bp: 4500,
  ask_quote: "forty-five percent of the balance",
  intent: "COUNTER",
  reason: "bp=3200",
  reason_text: "Counter at 32%: the first offer anchors at 70% of their ask.",
  counter_bp: 3200,
  nlg: llm("We can propose {counter_pct} of the balance, which is {offer_total}. Would that work?"),
  sentences: ["We can propose 32% of the balance, which is $400.00.", "Would that work?"],
  evalEv: evalAt(3200, 4),
  nextPhase: "NEGOTIATE",
  timings: { stt_ms: 380, queue_ms: 0, nlu_ms: 744, engine_ms: 11.7, policy_ms: 0.2, nlg_ms: 512, tts_onset_ms: 310 },
});

// ---- turn 3: ask 42%, counter 37% (LLM phrasing blocked by guard, template fallback)
turn(3, {
  creditor: "Thirty-two is too low. I could do forty-two percent.",
  stance: "counter",
  ask_bp: 4200,
  ask_quote: "forty-two percent",
  intent: "COUNTER",
  reason: "bp=3700",
  reason_text: "Counter at 37%: concede half the gap toward their new ask.",
  counter_bp: 3700,
  nlg: llm("We can propose {counter_pct} of the balance, which is {offer_total}. Would that work?", {
    guards: [
      { stage: "template", ok: true, reason: null },
      { stage: "rendered", ok: false, reason: "number_not_from_facts" },
      { stage: "fallback", ok: true, reason: null },
    ],
    fallback_used: true,
  }),
  blocked: [{ stage: "rendered", reason: "number_not_from_facts", offending: ["forty"] }],
  sentences: ["We can propose 37% of the balance, which is $462.50.", "Would that work?"],
  evalEv: evalAt(3700, 4),
  nextPhase: "NEGOTIATE",
  timings: { stt_ms: 395, queue_ms: 0, nlu_ms: 790, engine_ms: 10.2, policy_ms: 0.2, nlg_ms: 604, tts_onset_ms: 305 },
});

// ---- turn 4: firm floor 40%, confirm schedule
turn(4, {
  creditor: "Forty percent is my floor. I can't go lower than that.",
  stance: "offer",
  ask_bp: 4000,
  ask_quote: "Forty percent is my floor",
  intent: "CONFIRM_SCHEDULE",
  reason: "bp=4000",
  reason_text: "Accept 40%: their stated floor is one the client can afford, so read back the schedule.",
  counter_bp: 4000,
  nlg: llm(
    "{settlement_pct} works for us. We can do {num_payments} payments totaling {offer_total}, starting {first_payment_date}. Would that work?",
  ),
  sentences: [
    "40% works for us.",
    "We can do 5 payments totaling $500.00, starting November 2.",
    "Would that work?",
  ],
  evalEv: evalAt(4000, 5),
  nextPhase: "CONFIRM",
  timings: { stt_ms: 402, queue_ms: 0, nlu_ms: 701, engine_ms: 12.3, policy_ms: 0.3, nlg_ms: 548, tts_onset_ms: 298 },
});

const finalEval = lastEval;

// ---- turn 5: accept → PROPOSE_WRAP; agreement after acks
turn(5, {
  creditor: "Yes, that works for us.",
  stance: "accept",
  intent: "PROPOSE_WRAP",
  reason: "confirmed",
  reason_text: "They accepted the read-back schedule, so send it to the client for approval.",
  nlg: tmpl(
    "I have sent this proposal to the client for approval. Do you need anything else before we end the call?",
  ),
  sentences: [
    "I have sent this proposal to the client for approval.",
    "Do you need anything else before we end the call?",
  ],
  nextPhase: "WRAP",
  agreement: {
    type: "agreement",
    creditor: "NorthPeak Collections",
    bp: 4000,
    offer_total: 50000,
    status: "pending_client_approval",
    assumed_fields: ["first_payment_date"],
    rows: finalEval.rows,
  },
  timings: { stt_ms: 350, queue_ms: 0, nlu_ms: 455, engine_ms: 0.4, policy_ms: 0.2, nlg_ms: 0.3, tts_onset_ms: 300 },
});

// ---- turn 6: close
turn(6, {
  creditor: "No, that's everything. Thanks.",
  stance: "accept",
  intent: "CLOSE",
  reason: "thanks_accept",
  reason_text: "The rep has nothing else, so close the call politely.",
  nlg: tmpl("Thank you for your time. Goodbye."),
  sentences: ["Thank you for your time.", "Goodbye."],
  nextPhase: "END",
  timings: { stt_ms: 330, queue_ms: 0, nlu_ms: 402, engine_ms: 0.3, policy_ms: 0.1, nlg_ms: 0.2, tts_onset_ms: 295 },
});

const fixture = {
  _comment:
    "Synthetic, hand-specified easy_deal call on the operator stream (all fields). Each frame is {t: ms since start, ev: ServerEvent}. PRIVATE values: max_bp 5200, program fees, bank fees, SDA balances, the affordability curve.",
  scenario_id: "easy_deal",
  view: "operator",
  private_values: {
    max_bp: MAX_BP,
    program_fee_cents_per_row: FEE_ROW,
    bank_fee_cents: BANK,
    balance_cents: rows(5, 50000).map((r) => r.balance_cents),
  },
  frames,
};
writeFileSync(out, JSON.stringify(fixture, null, 1) + "\n");
console.log(`${frames.length} frames, ${t} ms`);
