/**
 * Phase 48 trace wording: ladder moves by name, handoffs, the total-or-per-
 * payment question with its cause, acks, corrections and dropped questions
 * from `turn_trace.notes`, the model badge from `turn_trace.reader`, and a
 * true reason for every dropped term.
 */
import { describe, expect, it } from "vitest";
import {
  ackLine,
  heardLines,
  ignoredLines,
  ladderMove,
  modelName,
  noteLine,
  readerLabel,
  turnTitle,
} from "@/lib/traceStory";
import { fullCall } from "@/test/fixtureState";
import type { Intent, TraceNote, TurnTraceEvent } from "@/types/protocol";

const base = fullCall("operator").traces.find((t) => t.turn === 2)!;

function tr(turn: number, intent: Intent, reason: string | null, bp: number | null, extra: Partial<TurnTraceEvent> = {}): TurnTraceEvent {
  return { ...base, turn, counter_bp: bp, decide: { ...base.decide, intent, reason }, notes: null, reader: null, dropped: [], ...extra };
}

describe("ladder moves", () => {
  // anchor → hold → step → (rep firm) final counter → accept on repeat, as haggling_rep plays it.
  const call = [
    tr(2, "COUNTER", "bp=4900", 4900),
    tr(3, "COUNTER", "hold", 4900),
    tr(4, "COUNTER", "step", 5900),
    tr(5, "COUNTER", "final_counter", 6200),
    tr(6, "CONFIRM_SCHEDULE", "rep_firm", 6500),
  ];

  it("names every step of the haggling ladder in plain words", () => {
    expect(call.map((t) => turnTitle(t, call))).toEqual([
      "Made a first offer of 49%",
      "Held our offer at 49%",
      "Took a step up to 59%",
      "Made a final offer of 62%",
      "Accepted 65% when they repeated it",
    ]);
  });

  it("tells a concession apart from the first offer, and the other accepts", () => {
    const first = tr(2, "COUNTER", "bp=3200", 3200);
    const concede = tr(3, "COUNTER", "bp=3700", 3700);
    expect(ladderMove(concede, [first])).toBe("concede");
    expect(turnTitle(concede, [first])).toBe("Raised our offer by half their drop, to 37%");
    expect(turnTitle(tr(7, "CONFIRM_SCHEDULE", "rep_held", 4000))).toBe("Accepted 40% after they held firm");
    expect(turnTitle(tr(7, "CONFIRM_SCHEDULE", "counters_exhausted", 4000))).toBe("Accepted 40%: no counteroffers left");
    const took = tr(4, "CONFIRM_SCHEDULE", "bp=3700", 3700, { stance: "accept" });
    expect(turnTitle(took, [first, concede])).toBe("They accepted our 37% offer");
    // A confirm of the rep's own number is still an accept.
    expect(turnTitle(tr(4, "CONFIRM_SCHEDULE", "bp=4000", 4000, { stance: "offer" }), [first, concede])).toBe("Accepted 40%");
  });

  it("calls every no-deal ending a handoff, and the dead NO_DEAL_WRAP a close", () => {
    expect(turnTitle(tr(6, "ESCALATE", "above_accept_line", null))).toBe("Handed off to a specialist");
    expect(turnTitle(tr(6, "NO_DEAL_WRAP", "infeasible", null))).toBe("Closed the call");
  });
});

const held: TraceNote = { kind: "amount_held", cents: 42000, quote: "$420", trigger: "total_pay_by" };

describe("notes from the server", () => {
  it("asks total-or-per-payment with the amount in the title and the cause in what we heard", () => {
    const t = tr(3, "CLARIFY", "amount_meaning", null, { ask_bp: null, terms: [], notes: [held] });
    expect(turnTitle(t)).toBe("Asked whether $420 is the total or per payment");
    expect(heardLines(t)).toEqual([
      "Held back $420 until they say whether it is the total or each payment, because “pay $420 by” a date can mean the whole amount or each payment",
    ]);
    const count = noteLine({ ...held, trigger: "total_with_count" });
    expect(count).toMatch(/because they named a number of payments in the same sentence, so it could be per payment$/);
    expect(turnTitle(tr(4, "CLARIFY", "amount_meaning", null))).toBe("Asked whether their amount is the total or per payment");
    expect(turnTitle(tr(4, "CLARIFY", "cents_ambiguity", null))).toBe("Asked whether they meant dollars or cents");
  });

  it("tells corrections, disputes, answers and dropped questions in plain words", () => {
    expect(noteLine({ kind: "ack_corrected", field: "max_payments", old_value: 5, new_value: 6 })).toBe(
      "Corrected what we repeated back: up to 6 payments (we had 5)",
    );
    expect(noteLine({ kind: "ack_disputed", field: "max_payments", old_value: 6, new_value: 6 })).toBe(
      "Said we misheard the max payments, so we check it with them again",
    );
    expect(noteLine({ kind: "amount_clarify_resolved", total: 42000 })).toBe("Said $420 is the total settlement");
    expect(noteLine({ kind: "amount_clarify_resolved", new_value: 42000 })).toBe("Said $420 is the minimum for each payment");
    expect(noteLine({ kind: "amount_clarify_dropped", cents: 42000, reason: "new_terms" })).toBe(
      "Moved on with new terms, so we dropped our question about $420",
    );
    expect(noteLine({ kind: "amount_clarify_dropped", cents: 42000, reason: "interrupt" })).toBe(
      "Raised something more pressing, so we dropped our question about $420",
    );
    expect(noteLine({ kind: "cents_clarify_dropped", field: "min_payment_cents", reason: "new_terms" })).toBe(
      "Moved on with new terms, so we dropped our dollars-or-cents question about the minimum payment",
    );
    expect(noteLine({ kind: "acked", fields: ["max_payments"] })).toBeNull();
  });

  it("does not say a corrected term twice", () => {
    const t = tr(2, "ASK", "min_payment_cents", null, {
      ask_bp: null,
      terms: [{ field: "max_payments", value: 6, quote: "six", verified: true, hedged: false }],
      notes: [{ kind: "ack_corrected", field: "max_payments", old_value: 5, new_value: 6 }],
    });
    expect(heardLines(t)).toEqual(["Corrected what we repeated back: up to 6 payments (we had 5)"]);
  });

  it("says when code opened the reply with an acknowledgement", () => {
    const t = tr(2, "ASK", "min_payment_cents", null, { notes: [{ kind: "acked", fields: ["max_payments"], total: null }] });
    expect(ackLine(t)).toBe(
      "The reply opens by repeating back what the rep just said (max payments). Code builds that sentence from the rep's own words, before the template below.",
    );
    expect(ackLine(tr(2, "ASK", null, null, { notes: [{ kind: "acked", fields: [], total: 42000 }] }))).toMatch(/\(\$420 in total\)/);
    expect(ackLine(tr(2, "ASK", null, null))).toBeNull();
  });
});

describe("dropped terms", () => {
  const drop = (reason: string, field = "max_payments", value: unknown = 6, quote: string | null = "six") =>
    ignoredLines(tr(2, "ASK", null, null, { dropped: [{ field, value, reason, quote }] }))[0];

  it("give the true reason for each check", () => {
    expect(drop("rejected_quote")).toBe("Ignored “Up to 6 payments”: those words are not in what the rep said");
    expect(drop("rejected_amount_value", "settlement_ask_total_cents", 42000, "$420")).toBe(
      "Ignored “$420 in total”: the dollar amount does not match the rep's words",
    );
    expect(drop("rejected_ask_value", "settlement_ask_pct", 100, "100%")).toMatch(/^Ignored “100% of the balance”: the percentage/);
    expect(drop("rejected_range", "min_payment_cents", 5, "5")).toMatch(/outside anything a rep would mean$/);
    expect(drop("rejected_bare_year", "first_payment_date", "2027-01-01", "2027")).toMatch(/a year alone is not a payment date$/);
    expect(drop("rejected_structure", "payment_structure", "flexible", "flexible")).toMatch(
      /the word was not about how the payments are structured$/,
    );
    expect(drop("something_new")).toMatch(/did not pass the check on the rep's words$/);
  });

  it("say a held-back amount was held back to ask, not that it failed to match", () => {
    const cents = drop("cents_ambiguity", "min_payment_cents", 150, "one fifty");
    expect(cents).toBe("Held back the minimum payment (“one fifty”): it was unclear whether they meant dollars or cents, so we ask");
    expect(cents).not.toMatch(/did not match/);
    expect(drop("tiers_ambiguous", "min_payment_tiers", [[1, 5000]], null)).toMatch(/^Held back their minimum payment rules/);
  });
});

describe("model badge", () => {
  it("names the reader in plain words", () => {
    const llm = { kind: "llm" as const, provider: "anthropic", model: "claude-haiku-5-5", fallback: false, budget_reached: false, cache_hit: false };
    expect(readerLabel(llm)).toBe("Read by Claude Haiku");
    expect(readerLabel({ ...llm, provider: "groq", model: "openai/gpt-oss-120b", fallback: true })).toBe("Read by Groq (fallback)");
    expect(readerLabel({ ...llm, provider: "groq", model: "openai/gpt-oss-120b", fallback: true, budget_reached: true })).toBe(
      "Budget reached, using Groq",
    );
    expect(readerLabel({ ...llm, cache_hit: true })).toBe("Read by Claude Haiku (cached)");
    expect(readerLabel({ kind: "code" })).toBe("Read by code (no AI needed)");
    expect(readerLabel({ kind: "script" })).toBe("Simulated rep: no AI reading");
    expect(readerLabel(null)).toBeNull();
    expect(modelName("anthropic", "claude-sonnet-5-5")).toBe("Claude Sonnet");
    expect(modelName("cerebras", "gpt-oss-120b")).toBe("Cerebras");
  });
});
