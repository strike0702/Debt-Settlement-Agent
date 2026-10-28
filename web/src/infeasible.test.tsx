/**
 * Why the rep's ask does not fit (Phase 60): the trace row "Why it does not fit" says
 * the engine's reason in plain words, with the shortfall and the date it binds, and
 * a Private tag. Debt negotiator view only; the Creditor rep view shows no trace.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DecisionTrace } from "@/components/DecisionTrace";
import { easyDeal } from "@/fixtures";
import { infeasibleLine } from "@/lib/traceStory";
import type { TraceInfeasible, TurnTraceEvent } from "@/types/events";

afterEach(cleanup);

const base = easyDeal.frames.map((f) => f.ev).find((e): e is TurnTraceEvent => e.type === "turn_trace" && e.creditor_text != null)!;

const why = (w: Partial<TraceInfeasible>): TurnTraceEvent => ({
  ...base,
  infeasible_reason: { reason: "cash_short", bp: 5200, binding_date: "2027-03-31", shortfall_cents: 14000, closest_k: 3, ...w },
});

describe("why the ask does not fit", () => {
  it("says each reason in plain words", () => {
    expect(infeasibleLine(why({}))).toBe(
      "At 52% the client's savings run short by $140 on March 31, 2027. The nearest plan has 3 payments.",
    );
    expect(infeasibleLine(why({ reason: "fee_short", bp: 4500, binding_date: "2026-11-30", shortfall_cents: 17500, closest_k: 1 }))).toBe(
      "At 45% the client's savings fall $175 short of the program fee by November 30, 2026. The nearest plan has 1 payment.",
    );
    expect(infeasibleLine(why({ reason: "floors", bp: 4000, shortfall_cents: 3000, closest_k: 1 }))).toBe(
      "At 40% the settlement is $30 below the creditor's minimum payment.",
    );
    expect(infeasibleLine(why({ reason: "segments", bp: 3600, shortfall_cents: 2, max_segments: 2, binding_date: "2027-06-28" }))).toBe(
      "At 36% no plan fits the creditor's limit of 2 different payment amounts: the client's savings run short by $0.02 on June 28, 2027. One more payment amount would fit.",
    );
    expect(infeasibleLine(why({ reason: "horizon", bp: 4500, binding_date: "2027-06-30", shortfall_cents: null, closest_k: null }))).toBe(
      "At 45% no payment can be scheduled: the first payment date, June 30, 2027, is after the client's savings plan ends.",
    );
    expect(infeasibleLine({ ...base, infeasible_reason: null })).toBeNull();
    expect(infeasibleLine({ ...base, infeasible_reason: undefined })).toBeNull();
  });

  it("shows the line in the open trace row with a Private tag", () => {
    render(<DecisionTrace traces={[why({})]} lens="operator" />);
    expect(screen.getByText("Why it does not fit")).toBeInTheDocument();
    const line = screen.getByTestId("infeasible-line");
    expect(line).toHaveTextContent("At 52% the client's savings run short by $140 on March 31, 2027.");
    expect(line.parentElement).toHaveTextContent(/private/i);
  });

  it("is not in the Creditor rep view", () => {
    render(<DecisionTrace traces={[why({})]} lens="creditor" />);
    expect(screen.queryByText("Why it does not fit")).toBeNull();
    expect(screen.queryByText(/\$140/)).toBeNull();
  });

  it("leaves the row out when the ask fits", () => {
    render(<DecisionTrace traces={[{ ...base, infeasible_reason: null }]} lens="operator" />);
    expect(screen.queryByText("Why it does not fit")).toBeNull();
  });
});
