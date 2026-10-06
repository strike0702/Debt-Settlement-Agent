import { describe, expect, it } from "vitest";
import { easyDeal } from "@/fixtures";
import { foldCall, ladderPoints } from "@/lib/callState";
import { micReducer } from "@/lib/mic";
import { splitByQuotes, splitTemplate } from "@/lib/highlight";
import { money, ordinal, pct, termValue } from "@/lib/format";
import { micFromEvents } from "@/App";

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
  it("renders tiers like the rep view (Phase 20 F10)", () => {
    expect(termValue("min_payment_tiers", [])).toBe("No special tiers");
    expect(termValue("min_payment_tiers", [[4, 7500], [8, 5000]])).toBe(
      "$75.00 from the 4th payment and $50.00 from the 8th payment",
    );
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
