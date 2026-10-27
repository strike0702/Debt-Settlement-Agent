/**
 * Minimum client cushion (Phase 59 follow-up): the money field shows only in the
 * Debt negotiator view before a call, is pre-filled with the scenario's firm default,
 * sends an edited value in `start.min_cushion_cents` (this call only), blocks a start
 * on an invalid value, and the decision trace says whether the plan kept it.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { DecisionTrace } from "@/components/DecisionTrace";
import { easyDeal } from "@/fixtures";
import { formatCushion, parseCushion } from "@/lib/cushion";
import { cushionLine } from "@/lib/traceStory";
import type { ScenarioBrief, ScenarioMeta, TurnTraceEvent } from "@/types/protocol";

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
}

const SCENARIOS: ScenarioMeta[] = [{ id: "easy_deal", title: "Easy deal", description: "d", expected: "deal", suggested: [] }];
const BRIEF = {
  id: "easy_deal",
  title: "Easy deal",
  description: "d",
  expected: "deal",
  creditor: { name: "NorthPeak Collections", creditor_balance_cents: 125000, original_balance_cents: 160000 },
  client: {
    as_of_date: "2026-03-01", sda_balance_cents: 44000, draft_amount_cents: 22000, draft_day: 15,
    first_draft_date: "2026-03-15", last_draft_date: "2026-04-15", upcoming_drafts: 2,
    upcoming_deposits_cents: 44000, upcoming_withdrawals_cents: 0, upcoming_ledger: [], ledger: [],
  },
  firm: { program_fee_bp: 1800, program_fee_cents: 28800, bank_fee_cents: 950, min_cushion_cents: 10000 },
} satisfies ScenarioBrief;
const REP = {
  id: "easy_deal",
  creditor: { name: "NorthPeak Collections", outstanding_balance_cents: 125000, original_balance_cents: 160000 },
  rules: { max_payments: 8, min_payment_cents: 10000, structure: "even", opening_ask_bp: 4500, floor_bp: 4000, first_payment: null },
};

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const fetchMock = vi.fn(async (url: string) => {
  if (url === "/scenarios") return json(SCENARIOS);
  if (url === "/scenarios/easy_deal") return json(BRIEF);
  if (url === "/scenarios/easy_deal/rep") return json(REP);
  return new Response("null", { status: 404 });
});

beforeEach(() => {
  FakeSocket.all = [];
  fetchMock.mockClear();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const field = () => screen.queryByLabelText("Minimum client cushion") as HTMLInputElement | null;
const startMsg = (ws: FakeSocket) => JSON.parse(ws.sent[0]!) as Record<string, unknown>;

describe("Minimum client cushion field", () => {
  it("is pre-filled with the firm default and not sent when unchanged", async () => {
    render(<App />);
    await screen.findByRole("button", { name: /Easy deal/ });
    expect(await screen.findByDisplayValue("100")).toBe(field());
    fireEvent.click(screen.getByRole("button", { name: /Start call/ }));
    const ws = FakeSocket.all.at(-1)!;
    act(() => ws.open());
    expect(startMsg(ws)).toEqual({ type: "start", scenario_id: "easy_deal" });
    // Gone while the call runs.
    expect(field()).toBeNull();
  });

  it("sends an edited value in start for this call (autoplay too)", async () => {
    render(<App />);
    await screen.findByDisplayValue("100");
    fireEvent.change(field()!, { target: { value: "250.50" } });
    fireEvent.click(screen.getByRole("button", { name: /Watch a call/ }));
    const ws = FakeSocket.all.at(-1)!;
    act(() => ws.open());
    expect(startMsg(ws)).toMatchObject({ type: "start", autoplay: true, min_cushion_cents: 25050 });
  });

  it("blocks a start on an invalid amount and says why", async () => {
    render(<App />);
    await screen.findByDisplayValue("100");
    fireEvent.change(field()!, { target: { value: "6000" } });
    expect(screen.getByText("Use $5,000 or less.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start call/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Watch a call/ })).toBeDisabled();
  });

  it("is never shown in the Creditor rep view", async () => {
    render(<App />);
    await screen.findByDisplayValue("100");
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(field()).toBeNull();
    expect(screen.queryByText(/client cushion/i)).toBeNull();
  });
});

describe("cushion parsing and trace line", () => {
  it("parses dollars into cents within 0 to $5,000", () => {
    expect(parseCushion("")).toEqual({ cents: 0 });
    expect(parseCushion("$1,250.5")).toEqual({ cents: 125050 });
    expect(parseCushion("5000")).toEqual({ cents: 500000 });
    expect(parseCushion("5000.01")).toHaveProperty("error");
    expect(parseCushion("-3")).toHaveProperty("error");
    expect(parseCushion("abc")).toHaveProperty("error");
    expect(formatCushion(10000)).toBe("100");
    expect(formatCushion(12345)).toBe("123.45");
  });

  const base = easyDeal.frames.map((f) => f.ev).find((e): e is TurnTraceEvent => e.type === "turn_trace" && e.decide.intent === "CONFIRM_SCHEDULE")!;

  it("says whether the plan keeps the cushion, in plain words", () => {
    expect(cushionLine({ ...base, cushion: { min_cushion_cents: 10000, kept: true } })).toBe(
      "The plan keeps at least $100 in the client's account.",
    );
    expect(cushionLine({ ...base, cushion: { min_cushion_cents: 10000, kept: false } })).toBe(
      "No plan at this price keeps $100 in the client's account, so the agent used one that goes lower.",
    );
    expect(cushionLine({ ...base, cushion: null })).toBeNull();
  });

  it("shows the line in the open trace row", () => {
    render(<DecisionTrace traces={[{ ...base, cushion: { min_cushion_cents: 10000, kept: false } }]} lens="operator" />);
    expect(screen.getByText("Client cushion")).toBeInTheDocument();
    expect(screen.getByTestId("cushion-line")).toHaveTextContent("No plan at this price keeps $100");
  });
});
