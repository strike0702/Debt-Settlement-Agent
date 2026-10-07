/**
 * LiveApp scenario picker over mocked `fetch` and `WebSocket`: the picker is
 * locked while a call runs and unlocks once the call is finished, even though
 * a finished autoplay call leaves its socket open (Phase 23b fix). The
 * Creditor rep view swaps the operator brief for the rep's own account
 * (Phase 34). Phase 35: view names, the client ledger in the Debt negotiator
 * view, no decision trace in the Creditor rep view, and a reason (not a bare
 * "No engine run") when a call streams the rep view.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import type { RepAccount, ScenarioBrief, ScenarioMeta, ServerEvent } from "@/types/protocol";

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(): void {}
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  emit(ev: ServerEvent) {
    this.onmessage?.({ data: JSON.stringify(ev) });
  }
}

const SCENARIOS: ScenarioMeta[] = [
  { id: "easy_deal", title: "Easy deal", description: "d", expected: "deal", suggested: [] },
  { id: "no_space", title: "No space", description: "d", expected: "no_deal", suggested: [] },
];

const REP: RepAccount = {
  id: "easy_deal",
  creditor: { name: "NorthPeak Collections", outstanding_balance_cents: 125000, original_balance_cents: 160000 },
  rules: { max_payments: 8, min_payment_cents: 10000, structure: "even", opening_ask_bp: 4500, floor_bp: 4000, first_payment: null },
};

const BRIEF: ScenarioBrief = {
  id: "easy_deal",
  title: "Easy deal",
  description: "d",
  expected: "deal",
  creditor: { name: "NorthPeak Collections", creditor_balance_cents: 125000, original_balance_cents: 160000 },
  client: {
    as_of_date: "2026-03-01",
    sda_balance_cents: 44000,
    draft_amount_cents: 22000,
    draft_day: 15,
    first_draft_date: "2026-03-15",
    last_draft_date: "2026-04-15",
    upcoming_drafts: 2,
    upcoming_deposits_cents: 44000,
    upcoming_withdrawals_cents: 0,
    upcoming_ledger: [],
    ledger: [
      { date: "2026-03-15", amount_cents: 22000, type: "credit", scheduled: true },
      { date: "2026-04-15", amount_cents: 22000, type: "credit", scheduled: true },
    ],
  },
  firm: { program_fee_bp: 1800, program_fee_cents: 28800, bank_fee_cents: 950 },
};

const fetchMock = vi.fn(async (url: string) =>
  url === "/scenarios"
    ? new Response(JSON.stringify(SCENARIOS), { status: 200 })
    : url === "/scenarios/easy_deal/rep"
      ? new Response(JSON.stringify(REP), { status: 200 })
      : url === "/scenarios/easy_deal"
        ? new Response(JSON.stringify(BRIEF), { status: 200 })
        : new Response("null", { status: 404 }),
);

beforeEach(() => {
  FakeSocket.all = [];
  fetchMock.mockClear();
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const card = (title: string) => screen.getByRole("button", { name: new RegExp(title) });

describe("LiveApp scenario picker", () => {
  it("is locked during an autoplay call and unlocks after autoplay_done", async () => {
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Watch a call/ }));
    const ws = FakeSocket.all.at(-1)!;
    act(() => ws.open());

    fireEvent.click(card("No space"));
    expect(card("Easy deal")).toHaveAttribute("aria-pressed", "true");

    act(() =>
      ws.emit({ type: "autoplay_done", final_intent: "CLOSE", outcome: "deal", phase: "END", turns: 6 } as ServerEvent),
    );
    fireEvent.click(card("No space"));
    expect(card("No space")).toHaveAttribute("aria-pressed", "true");
    expect(card("Easy deal")).toHaveAttribute("aria-pressed", "false");
  });
});

describe("LiveApp Creditor rep view", () => {
  it("shows the rep's own account instead of the operator brief", async () => {
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();
    expect(screen.queryByText("Your account")).not.toBeInTheDocument();
    const briefCalls = () => fetchMock.mock.calls.filter(([u]) => u === "/scenarios/easy_deal").length;
    const before = briefCalls();

    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(await screen.findByText("Up to 8 payments")).toBeInTheDocument();
    expect(screen.getByText("Your account")).toBeInTheDocument();
    expect(screen.getByText("Floor 40% — don't go below")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/scenarios/easy_deal/rep");
    // The private brief is never requested in the Creditor rep view.
    expect(briefCalls()).toBe(before);
  });

  it("has no decision trace column and no client ledger (Phase 35)", async () => {
    render(<App />);
    expect(await screen.findByText("Client deposits and credits")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(await screen.findByText("Your account")).toBeInTheDocument();
    expect(screen.queryByText("Decision trace")).not.toBeInTheDocument();
    expect(screen.queryByText("Client deposits and credits")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("$220.00");
  });
});

describe("LiveApp view names and Debt negotiator view (Phase 35)", () => {
  it("names the views in plain words with a one-line subtitle", async () => {
    render(<App />);
    expect(await screen.findByRole("radiogroup", { name: "View" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Debt negotiator" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText(/Operator|Creditor's eye/)).not.toBeInTheDocument();
    expect(screen.getByTestId("view-hint")).toHaveTextContent(/agent's side/);
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(screen.getByTestId("view-hint")).toHaveTextContent(/creditor's representative/);
  });

  it("shows the client ledger next to the brief", async () => {
    render(<App />);
    expect(await screen.findByText("Client deposits and credits")).toBeInTheDocument();
    expect(screen.getByText("Balance today (as of)")).toBeInTheDocument();
    expect(screen.getByText("$880.00")).toBeInTheDocument();
  });

  it("explains an empty trace when the call was started in the Creditor rep view", async () => {
    // Regression: the user saw "No engine run this turn." on every turn here.
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    fireEvent.click(screen.getByRole("button", { name: /Watch a call/ }));
    const ws = FakeSocket.all.at(-1)!;
    expect(ws.url).toContain("view=rep");
    act(() => ws.open());
    act(() => ws.emit({ type: "transcript", role: "agent", text: "Hello.", spoken: true, sentence_id: "s1", blocked: false }));
    fireEvent.click(screen.getByRole("radio", { name: "Debt negotiator" }));
    expect(await screen.findByText(/which carries no decision trace/)).toBeInTheDocument();
    expect(screen.queryByText("No engine run this turn.")).not.toBeInTheDocument();
  });
});
