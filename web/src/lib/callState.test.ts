import { describe, expect, it } from "vitest";
import { easyDeal } from "@/fixtures";
import { foldCall, ladderPoints, withOperatorDetail } from "@/lib/callState";
import { micReducer } from "@/lib/mic";
import { splitByQuotes, splitTemplate } from "@/lib/highlight";
import { money, ordinal, pct, termValue } from "@/lib/format";
import { eventsForLens, micFromEvents } from "@/App";
import type { AuditEvent, TurnTraceEvent } from "@/types/protocol";

const events = easyDeal.frames.map((f) => f.ev);

describe("foldCall over the fixture", () => {
  const s = foldCall(events);
  it("ends the call with an agreement", () => {
    expect(s.phase).toBe("END");
    expect(s.agreement?.bp).toBe(4000);
    expect(s.agreement?.offer_total).toBe(50000);
  });
  it("groups consecutive sentences into one bubble per speaker turn", () => {
    expect(s.messages.map((m) => m.role)).toEqual([
      "agent", "creditor", "agent", "creditor", "agent", "creditor", "agent",
      "creditor", "agent", "creditor", "agent", "creditor", "agent",
    ]);
  });
  it("builds the ladder from asks and counters", () => {
    expect(ladderPoints(s.traces)).toEqual([
      { turn: 2, ask: 4500, counter: 3200 },
      { turn: 3, ask: 4200, counter: 3700 },
      { turn: 4, ask: 4000, counter: 4000 },
    ]);
  });
  it("frames are time-ordered", () => {
    const ts = easyDeal.frames.map((f) => f.t);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
  });
});

describe("mic state machine", () => {
  it("cycles listening → thinking → speaking → listening", () => {
    let m = micReducer("off", { type: "toggle" });
    expect(m).toBe("listening");
    m = micReducer(m, { type: "rep_done" });
    expect(m).toBe("thinking");
    m = micReducer(m, { type: "agent_say" });
    expect(m).toBe("speaking");
    expect(micReducer(m, { type: "barge_in" })).toBe("listening");
    expect(micReducer("off", { type: "agent_say" })).toBe("off");
  });
  it("is off after the replayed call ends", () => {
    expect(micFromEvents(events)).toBe("off");
  });
});

describe("formatting and highlighting", () => {
  it("formats money en-US and percentages from bp", () => {
    expect(money(125000)).toBe("$1,250.00");
    expect(pct(4500)).toBe("45%");
    expect(pct(4250)).toBe("42.5%");
  });
  it("renders tiers in the agent's spoken style (Phase 20 F10, 23a.5)", () => {
    expect(termValue("min_payment_tiers", [])).toBe("No special tiers");
    expect(termValue("min_payment_tiers", [[4, 7500], [8, 5050]])).toBe(
      "$75 from the 4th payment and $50.50 from the 8th payment",
    );
    // F10/F11 (ported from test_app_js_contracts): en-US money, never "US$", never a raw "[]".
    expect(money(123456)).toBe("$1,234.56");
    expect(money(123456)).not.toContain("US$");
    expect(termValue("min_payment_tiers", [])).not.toBe("[]");
    expect([1, 2, 3, 11, 12, 22].map(ordinal)).toEqual(["1st", "2nd", "3rd", "11th", "12th", "22nd"]);
  });
  it("highlights quotes case-insensitively and merges overlaps", () => {
    expect(splitByQuotes("Forty percent is my floor.", ["forty percent", "percent is my"])).toEqual([
      { text: "Forty percent is my", hit: true },
      { text: " floor.", hit: false },
    ]);
  });
  it("splits template placeholders", () => {
    expect(splitTemplate("Pay {offer_total} now").filter((s) => s.hit).map((s) => s.text)).toEqual(["{offer_total}"]);
  });
});

describe("withOperatorDetail (Phase 36)", () => {
  it("restores the trace, private audit and fee columns over a rep-stream fold", () => {
    const op = foldCall(events);
    const rep = foldCall([...eventsForLens(events, "creditor"), { type: "tts_onset", turn: 2, ms: 120 }]);
    expect(rep.traces).toEqual([]);
    const traces = events.filter((e): e is TurnTraceEvent => e.type === "turn_trace");
    const audit = events.filter((e): e is AuditEvent => e.type === "audit");
    const merged = withOperatorDetail(rep, { call_id: "c", traces, audit, eval: op.evaluation, agreement: op.agreement });
    expect(merged.traces.map((t) => t.turn)).toEqual(op.traces.map((t) => t.turn));
    expect(merged.traces.find((t) => t.turn === 2)?.timings.tts_onset_ms).toBe(120);
    expect(merged.audit.length).toBe(op.audit.length);
    expect(merged.audit.some((a) => a.private)).toBe(true);
    expect(merged.evaluation).toEqual(op.evaluation);
    expect(merged.agreement).toEqual(op.agreement);
    // The live stream keeps the conversation.
    expect(merged.messages).toEqual(rep.messages);
  });
});
