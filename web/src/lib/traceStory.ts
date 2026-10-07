/**
 * A turn trace retold in plain words for a newcomer (Phase 36).
 *
 * The server's `turn_trace` is written for engineers: intent codes, belief
 * statuses, guard stages, a template with placeholders. `DecisionTrace` takes
 * every sentence it shows from here (plus the server's own reason text): a
 * turn title ("Countered at 37%"), what the agent heard ("Up to 8 payments"),
 * whether the client can afford it, and the safety checks, each listed once
 * (the server repeats a check per sentence). Pure; no React.
 */
import { fieldLabel, isoDate, moneyShort, pct, termValue } from "@/lib/format";
import type { TurnTraceEvent } from "@/types/protocol";

/** A short past-tense title for the agent's move, with its number when it has one. */
export function turnTitle(t: TurnTraceEvent): string {
  const bp = t.counter_bp ?? t.ask_bp;
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
      return "Asked the rep to clarify";
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
    case "CLOSE":
      return "Closed the call";
    case "NO_DEAL_WRAP":
      return "Ended without a deal";
    case "ESCALATE":
      return "Handed the call to a person";
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
    default:
      return `${fieldLabel(field)}: ${termValue(field, value)}`;
  }
}

/** What the agent took from the rep's line (empty when nothing). */
export function heardLines(t: TurnTraceEvent): string[] {
  const out: string[] = [];
  if (t.ask_bp != null) out.push(`They want ${pct(t.ask_bp)} of the balance`);
  for (const term of t.terms) out.push(termPhrase(term.field, term.value) + (term.hedged ? " (not firm yet)" : ""));
  return out;
}

/** Terms the agent heard but did not trust, in plain words. */
export function ignoredLines(t: TurnTraceEvent): string[] {
  return t.dropped.map((d) => `Ignored “${termPhrase(d.field, d.value)}”: it did not match the rep's words`);
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
