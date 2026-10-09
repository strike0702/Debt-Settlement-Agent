/**
 * A turn trace retold in plain words for a newcomer (Phase 36).
 *
 * The server's `turn_trace` is written for engineers: intent codes, belief
 * statuses, guard stages, a template with placeholders. `DecisionTrace` takes
 * every sentence it shows from here (plus the server's own reason text): a
 * turn title ("Countered at 37%"), what the agent heard ("Up to 8 payments"),
 * whether the client can afford it, and the safety checks, each listed once
 * (the server repeats a check per sentence). Pure; no React.
 *
 * Phase 48: price moves are named by their step on the ladder ("Held our
 * offer at 42%", "Made a final offer of 48%", "Accepted 50% when they
 * repeated it"), a handoff is "Handed off to a specialist", and the server's
 * `reader` / `notes` become "Read by Claude Haiku" and plain lines about acks,
 * corrections, held-back amounts and dropped questions. Each dropped term says
 * why it was dropped (`DroppedTerm.reason`).
 */
import { fieldLabel, isoDate, moneyShort, pct, termValue } from "@/lib/format";
import type { TraceNote, TraceReader, TurnTraceEvent } from "@/types/protocol";

/** The outcome of every call that ends without a deal (Phase 45: deal or handoff). */
export const HANDOFF_TITLE = "Handed off to a specialist";

/** Where a price move sits on the ladder (policy reason codes, plus history for the first offer). */
export type LadderMove =
  | "first_offer"
  | "hold"
  | "step"
  | "concede"
  | "final_offer"
  | "accept_on_repeat"
  | "accept_after_hold"
  | "accept_out_of_counters"
  | "accept_our_offer";

/**
 * The ladder step of a COUNTER or CONFIRM_SCHEDULE move, or null. `earlier` is
 * the call's other traces (any order); only turns before `t` are read.
 */
export function ladderMove(t: TurnTraceEvent, earlier: readonly TurnTraceEvent[] = []): LadderMove | null {
  const before = earlier.filter((e) => e.turn < t.turn);
  const counters = before.filter((e) => e.decide.intent === "COUNTER" && e.counter_bp != null);
  const reason = t.decide.reason;
  if (t.decide.intent === "COUNTER") {
    if (reason === "hold") return "hold";
    if (reason === "step") return "step";
    if (reason === "final_counter") return "final_offer";
    return counters.length === 0 ? "first_offer" : "concede";
  }
  if (t.decide.intent === "CONFIRM_SCHEDULE") {
    if (reason === "rep_firm") return "accept_on_repeat";
    if (reason === "rep_held") return "accept_after_hold";
    if (reason === "counters_exhausted") return "accept_out_of_counters";
    const last = counters.sort((a, b) => a.turn - b.turn).at(-1);
    if (last && t.counter_bp != null && last.counter_bp === t.counter_bp && t.stance === "accept") return "accept_our_offer";
  }
  return null;
}

function ladderTitle(move: LadderMove, bp: number): string {
  const p = pct(bp);
  switch (move) {
    case "first_offer":
      return `Made a first offer of ${p}`;
    case "hold":
      return `Held our offer at ${p}`;
    case "step":
      return `Took a small step up to ${p}`;
    case "concede":
      return `Raised our offer by half their drop, to ${p}`;
    case "final_offer":
      return `Made a final offer of ${p}`;
    case "accept_on_repeat":
      return `Accepted ${p} when they repeated it`;
    case "accept_after_hold":
      return `Accepted ${p} after they held firm`;
    case "accept_out_of_counters":
      return `Accepted ${p}: no counteroffers left`;
    case "accept_our_offer":
      return `They accepted our ${p} offer`;
  }
}

/** The dollar amount the turn held back for "total or per payment?", if any. */
function heldAmount(t: TurnTraceEvent): number | null {
  const held = (t.notes ?? []).find((n) => n.kind === "amount_held" && n.cents != null);
  return held?.cents ?? null;
}

function clarifyTitle(t: TurnTraceEvent): string {
  switch (t.decide.reason) {
    case "amount_meaning": {
      const cents = heldAmount(t);
      return cents != null
        ? `Asked whether ${moneyShort(cents)} is the total or per payment`
        : "Asked whether their amount is the total or per payment";
    }
    case "cents_ambiguity":
      return "Asked whether they meant dollars or cents";
    case "tiers_ambiguous":
      return "Asked the rep to restate their minimums";
    default:
      return "Asked which value is right";
  }
}

/**
 * A short past-tense title for the agent's move, with its number when it has one.
 * `earlier` (the call's other traces) lets a counter say whether it is the first offer.
 */
export function turnTitle(t: TurnTraceEvent, earlier: readonly TurnTraceEvent[] = []): string {
  const bp = t.counter_bp ?? t.ask_bp;
  const move = ladderMove(t, earlier);
  if (move && t.counter_bp != null) return ladderTitle(move, t.counter_bp);
  switch (t.decide.intent) {
    case "OPENING":
      return "Opened the call";
    case "ASK":
      return "Asked for the payment terms";
    case "ASK_SETTLEMENT":
      return "Asked what they would settle for";
    case "READ_BACK":
      return "Checked a term with the rep";
    case "CLARIFY":
      return clarifyTitle(t);
    case "REFUSE_PRIVATE":
      return "Declined to share private details";
    case "REFUSE_COMMIT":
      return "Declined to commit on the call";
    case "COUNTER":
      return t.counter_bp != null ? `Countered at ${pct(t.counter_bp)}` : "Made a counteroffer";
    case "COUNTER_TERMS":
      return "Proposed different payment terms";
    case "CONFIRM_SCHEDULE":
      return bp != null ? `Accepted ${pct(bp)}` : "Proposed a payment schedule";
    case "SPEAK_SCHEDULE":
      return "Read out the payment schedule";
    case "PROPOSE_WRAP":
      return "Sent the deal to the client";
    // NO_DEAL_WRAP is dead since Phase 45 (every no-deal ending is a handoff); old logs read as a close.
    case "CLOSE":
    case "NO_DEAL_WRAP":
      return "Closed the call";
    case "ESCALATE":
      return HANDOFF_TITLE;
    case "ANSWER":
      return "Answered the rep's question";
  }
}

const STRUCTURE: Record<string, string> = {
  even: "All payments the same amount",
  balloon: "A larger final payment is fine",
  flexible: "Payment amounts can vary",
};

/** One term the rep stated, as a short sentence-case phrase. */
export function termPhrase(field: string, value: unknown): string {
  const n = typeof value === "number" ? value : null;
  switch (field) {
    case "max_payments":
      return n === 1 ? "A single payment" : `Up to ${n ?? "?"} payments`;
    case "min_payment_cents":
      return n != null ? `At least ${moneyShort(n)} per payment` : "A minimum payment";
    case "payment_structure":
      return STRUCTURE[String(value)] ?? `Structure: ${String(value)}`;
    case "first_payment_date":
      return typeof value === "string" ? `First payment by ${isoDate(value)}` : "A first payment date";
    case "max_segments":
      return n === 1 ? "One payment amount throughout" : `No more than ${n ?? "?"} different payment amounts`;
    case "max_token_pays":
      return `Up to ${n ?? "?"} small token payments`;
    case "min_payment_tiers":
      return Array.isArray(value) && value.length === 0 ? "No special minimums" : `Minimums: ${termValue(field, value)}`;
    case "settlement_ask_pct":
      return typeof value === "number" ? `${value}% of the balance` : "A settlement percentage";
    case "settlement_ask_total_cents":
      return n != null ? `${moneyShort(n)} in total` : "A total amount";
    case "amount_ambiguous_cents":
      return n != null ? moneyShort(n) : "A dollar amount";
    default:
      return `${fieldLabel(field)}: ${termValue(field, value)}`;
  }
}

const lower = (s: string) => (s ? s[0]!.toLowerCase() + s.slice(1) : s);

/** Why the agent held back an amount to ask "total or per payment?" (`nlu.resolve_amounts` triggers). */
const AMOUNT_WHY: Record<string, (amount: string) => string> = {
  nlu_flag: () => "the AI reading the line could not tell which it was",
  pct_total_disagree: () => "it did not match the percentage they gave",
  total_exceeds_balance: () => "as a total it would be more than the balance",
  min_exceeds_balance: () => "that much per payment would add up to more than the balance",
  total_cue: () => "they spoke of a total, but the amount was heard as a payment minimum",
  both_readings: () => "it was heard both as a total and as a payment minimum",
  total_pay_by: (a) => `“pay ${a} by” a date can mean the whole amount or each payment`,
  total_with_count: () => "they named a number of payments in the same sentence, so it could be per payment",
};

/** One plain line per server note, or null for notes the story does not tell here (`acked`). */
export function noteLine(n: TraceNote): string | null {
  const amount = n.cents != null ? moneyShort(n.cents) : "their amount";
  const field = n.field ? fieldLabel(n.field).toLowerCase() : "term";
  switch (n.kind) {
    case "ack_corrected":
      return `Corrected what we repeated back: ${lower(termPhrase(n.field ?? "", n.new_value))} (we had ${termValue(n.field ?? "", n.old_value)})`;
    case "ack_disputed":
      return `Said we misheard the ${field}, so we check it with them again`;
    case "amount_held": {
      const why = AMOUNT_WHY[n.trigger ?? ""]?.(amount) ?? "it could mean either";
      return `Held back ${amount} until they say whether it is the total or each payment, because ${why}`;
    }
    case "amount_clarify_resolved":
      if (n.total != null) return `Said ${moneyShort(n.total)} is the total settlement`;
      if (typeof n.new_value === "number") return `Said ${moneyShort(n.new_value)} is the minimum for each payment`;
      return "Answered our total-or-per-payment question";
    case "amount_clarify_dropped":
      return n.reason === "interrupt"
        ? `Raised something more pressing, so we dropped our question about ${amount}`
        : `Moved on with new terms, so we dropped our question about ${amount}`;
    case "cents_clarify_dropped":
      return `Moved on with new terms, so we dropped our dollars-or-cents question about the ${field}`;
    case "acked":
      return null;
  }
}

/** What the agent took from the rep's line (empty when nothing). */
export function heardLines(t: TurnTraceEvent): string[] {
  const out: string[] = [];
  if (t.ask_bp != null) out.push(`They want ${pct(t.ask_bp)} of the balance`);
  // A correction already names the new value; do not say the term twice.
  const corrected = new Set((t.notes ?? []).filter((n) => n.kind === "ack_corrected").map((n) => n.field));
  for (const term of t.terms) {
    if (corrected.has(term.field)) continue;
    out.push(termPhrase(term.field, term.value) + (term.hedged ? " (not firm yet)" : ""));
  }
  for (const n of t.notes ?? []) {
    const line = noteLine(n);
    if (line) out.push(line);
  }
  return out;
}

/** Why a term was dropped, keyed on `DroppedTerm.reason` (the NLU audit event minus `nlu_`). */
const DROP_WHY: Record<string, string> = {
  rejected_quote: "those words are not in what the rep said",
  rejected_amount_value: "the dollar amount does not match the rep's words",
  rejected_ask_value: "the percentage does not match their words or names something other than their ask",
  rejected_range: "the value is outside anything a rep would mean",
  rejected_date: "it is not a real date",
  rejected_bare_year: "a year alone is not a payment date",
  rejected_tiers: "the minimum payment rules could not be read",
};

/** Terms the agent heard but did not use, each with the true reason. */
export function ignoredLines(t: TurnTraceEvent): string[] {
  return t.dropped.map((d) => {
    const label = fieldLabel(d.field).toLowerCase();
    if (d.reason === "cents_ambiguity") {
      return `Held back the ${label}${d.quote ? ` (“${d.quote}”)` : ""}: it was unclear whether they meant dollars or cents, so we ask`;
    }
    if (d.reason === "tiers_ambiguous") {
      return "Held back their minimum payment rules: it was unclear which payment each minimum starts from, so we ask";
    }
    const why = DROP_WHY[d.reason] ?? "it did not pass the check on the rep's words";
    return `Ignored “${termPhrase(d.field, d.value)}”: ${why}`;
  });
}

const PROVIDER_NAME: Record<string, string> = {
  groq: "Groq",
  cerebras: "Cerebras",
  gemini: "Gemini",
  mistral: "Mistral",
  openrouter: "OpenRouter",
  ollama: "a local model",
  fake: "a test model",
};

/** "Claude Haiku" for anthropic/claude-haiku-5-5; otherwise the provider's name. */
export function modelName(provider: string | null | undefined, model: string | null | undefined): string {
  if (provider === "anthropic") {
    const family = /claude-(haiku|sonnet|opus|fable)/.exec(model ?? "")?.[1];
    return family ? `Claude ${family[0]!.toUpperCase()}${family.slice(1)}` : "Claude";
  }
  return PROVIDER_NAME[provider ?? ""] ?? provider ?? "an AI model";
}

/** The model badge for a rep turn ("Read by Claude Haiku"), or null when nothing is known. */
export function readerLabel(r: TraceReader | null | undefined): string | null {
  if (!r) return null;
  if (r.kind === "script") return "Simulated rep: no AI reading";
  if (r.kind === "code") return "Read by code (no AI needed)";
  const name = modelName(r.provider, r.model);
  const cached = r.cache_hit ? ", cached" : "";
  if (r.budget_reached) return `Budget reached, using ${name}${cached ? " (cached)" : ""}`;
  if (r.fallback) return `Read by ${name} (fallback${cached})`;
  return `Read by ${name}${cached ? " (cached)" : ""}`;
}

/** Hover text for the badge: the exact provider and model. */
export function readerDetail(r: TraceReader | null | undefined): string | undefined {
  if (!r || r.kind !== "llm") return undefined;
  return [r.provider, r.model].filter(Boolean).join("/");
}

/** When the reply opened with a code-built acknowledgement, says so in plain words (else null). */
export function ackLine(t: TurnTraceEvent): string | null {
  const ack = (t.notes ?? []).find((n) => n.kind === "acked");
  if (!ack) return null;
  const parts = [
    ...(ack.total != null ? [`${moneyShort(ack.total)} in total`] : []),
    ...(ack.fields ?? []).map((f) => fieldLabel(f).toLowerCase()),
  ];
  const what = parts.length > 0 ? ` (${parts.join(", ")})` : "";
  return `The reply opens by repeating back what the rep just said${what}. Code builds that sentence from the rep's own words, before the template below.`;
}

export type Afford = { tone: "good" | "bad" | "neutral"; text: string } | null;

/** Can the client afford it? PRIVATE (uses the engine's ceiling). Null when the engine had nothing to say. */
export function affordLine(t: TurnTraceEvent): Afford {
  const a = t.affordability;
  if (!a) {
    if (t.creditor_text == null) return null;
    const missing = t.needs_info ?? null;
    if (missing && missing.length > 0) {
      return {
        tone: "neutral",
        text: `Not checked yet: the agent still needs ${missing.map((f) => fieldLabel(f).toLowerCase()).join(" and ")}.`,
      };
    }
    return null;
  }
  const max = a.max_bp;
  if (max == null) return { tone: "bad", text: "No settlement fits the client's savings." };
  const target = t.counter_bp ?? t.ask_bp;
  const full = max >= 10000;
  if (target == null) {
    return { tone: "neutral", text: full ? "The client could afford even the full balance." : `The client can afford up to ${pct(max)} of the balance.` };
  }
  const ok = a.curve.find((p) => p.bp === target)?.feasible ?? target <= max;
  const who = t.counter_bp != null ? "" : "Their ";
  const most = full ? "even the full balance would" : `the most is ${pct(max)}`;
  return ok
    ? { tone: "good", text: `Yes. ${who}${pct(target)} fits the client's savings (${most}).` }
    : { tone: "bad", text: `No. ${who}${pct(target)} is more than the client can pay (the most is ${pct(max)}).` };
}

export interface CheckSummary {
  /** Distinct checks that passed (each stage once, however many sentences). */
  passed: string[];
  /** Distinct checks that blocked a wording, in plain words. */
  blocked: string[];
  fallback: boolean;
}

/** What each check makes sure of, as it reads in a list of passed checks. */
const CHECK_NAME: Record<string, string> = {
  template: "Uses an approved reply",
  unfilled: "Every blank filled in",
  rendered: "No unchecked numbers or promises",
};

/** One entry per check, never one per sentence (the server repeats stages per sentence). */
export function checkSummary(t: TurnTraceEvent): CheckSummary {
  const passed = new Set<string>();
  const failedStages = new Set<string>();
  const blocked = new Set<string>();
  for (const g of t.nlg.guards) {
    const name = CHECK_NAME[g.stage] ?? g.stage;
    if (g.ok) {
      passed.add(name);
    } else {
      failedStages.add(name);
      blocked.add(`${name}: stopped a line (${(g.reason ?? "no reason given").replaceAll("_", " ")})`);
    }
  }
  // A check that failed on one sentence is reported once, as failed.
  for (const name of failedStages) passed.delete(name);
  return { passed: [...passed], blocked: [...blocked], fallback: t.nlg.fallback_used };
}

export function spokenText(t: TurnTraceEvent): string {
  return t.spoken.map((s) => s.text).join(" ");
}

/** The key number of a turn for the compact list ("40%"), or null. */
export function keyNumber(t: TurnTraceEvent): string | null {
  const bp = t.counter_bp ?? t.ask_bp;
  return bp != null ? pct(bp) : null;
}
