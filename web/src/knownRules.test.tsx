/**
 * Known creditor rules panel (Phase 69): Debt negotiator view only, before a call;
 * pre-filled with the firm's saved rules and not sent when unchanged; entered rules go
 * in `start.known_rules` as cents ints; tiers that do not fit the other rules go red and
 * block Start; "Fill from scenario" copies the scenario's true rules; the rep view
 * never shows the panel or rules the operator entered.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { PANEL_HINT } from "@/components/KnownRulesPanel";
import { toRepView } from "@/lib/repView";
import type { BeliefEvent, ScenarioBrief, ScenarioMeta } from "@/types/protocol";

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
  firm: { program_fee_bp: 1800, program_fee_cents: 28800, bank_fee_cents: 950, min_cushion_cents: 0, known_rules: { max_payments: 6 } },
  scenario_rules: {
    max_payments: 8, min_payment_cents: 10000, payment_structure: "balloon", min_payment_tiers: [{ from_payment: 4, min_cents: 20000 }],
    max_segments: 2, max_token_pays: 8,
  },
} satisfies ScenarioBrief;

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const fetchMock = vi.fn(async (url: string) => {
  if (url === "/scenarios") return json(SCENARIOS);
  if (url === "/scenarios/easy_deal") return json(BRIEF);
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

const panel = () => screen.findByTestId("known-rules");
const input = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const startMsg = () => {
  fireEvent.click(screen.getByRole("button", { name: /Start call/ }));
  const ws = FakeSocket.all.at(-1)!;
  act(() => ws.open());
  return JSON.parse(ws.sent[0]!) as Record<string, unknown>;
};

describe("Known creditor rules panel", () => {
  it("is pre-filled with the firm's saved rules, green, and not sent when unchanged", async () => {
    render(<App />);
    await panel();
    expect(input("Max payments").value).toBe("6");
    expect(input("Max payments").parentElement).toHaveAttribute("data-state", "accepted");
    expect(input("Max payments").parentElement!.className).toMatch(/\bborder-good\b/);
    expect(startMsg()).toEqual({ type: "start", scenario_id: "easy_deal" });
    expect(screen.queryByTestId("known-rules")).toBeNull(); // gone while the call runs
  });

  it("sends what was entered as cents, grey while editing and green once accepted", async () => {
    render(<App />);
    await panel();
    fireEvent.change(input("Minimum payment"), { target: { value: "125.50" } });
    expect(input("Minimum payment").parentElement).toHaveAttribute("data-state", "editing");
    fireEvent.blur(input("Minimum payment"));
    expect(input("Minimum payment").parentElement).toHaveAttribute("data-state", "accepted");
    fireEvent.change(screen.getByLabelText("Payment structure"), { target: { value: "even" } });
    fireEvent.click(screen.getByRole("button", { name: /Add a tier/ }));
    fireEvent.change(input("Tier 1 from payment number"), { target: { value: "3" } });
    fireEvent.change(input("Tier 1 minimum payment"), { target: { value: "200" } });
    expect(startMsg()).toMatchObject({
      known_rules: {
        max_payments: 6,
        min_payment_cents: 12550,
        payment_structure: "even",
        min_payment_tiers: [{ from_payment: 3, min_cents: 20000 }],
      },
    });
  });

  it("marks tiers that do not fit the other rules red and blocks Start", async () => {
    render(<App />);
    await panel();
    fireEvent.click(screen.getByRole("button", { name: /Add a tier/ }));
    fireEvent.change(input("Tier 1 from payment number"), { target: { value: "9" } });
    fireEvent.change(input("Tier 1 minimum payment"), { target: { value: "200" } });
    expect(screen.getByRole("alert")).toHaveTextContent("after the last allowed payment (6)");
    expect(input("Tier 1 from payment number").parentElement).toHaveAttribute("data-state", "invalid");
    expect(input("Tier 1 from payment number")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: /Start call/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Remove tier 1" }));
    expect(screen.getByRole("button", { name: /Start call/ })).toBeEnabled();
  });

  it("fills from the scenario's true rules", async () => {
    render(<App />);
    await panel();
    fireEvent.click(screen.getByRole("button", { name: "Fill from scenario" }));
    expect(input("Max payments").value).toBe("8");
    expect(input("Minimum payment").value).toBe("100");
    expect((screen.getByLabelText("Payment structure") as HTMLSelectElement).value).toBe("balloon");
    expect(input("Tier 1 from payment number").value).toBe("4");
    expect(input("Tier 1 minimum payment").value).toBe("200");
    expect(startMsg()).toMatchObject({ known_rules: BRIEF.scenario_rules });
  });

  it("puts its description in a tooltip on the title, not under it", async () => {
    render(<App />);
    const card = await panel();
    const title = within(card).getByText("Known creditor rules");
    const tip = document.getElementById(title.getAttribute("aria-describedby")!)!;
    expect(tip).toHaveAttribute("role", "tooltip");
    expect(tip).toHaveTextContent(PANEL_HINT);
    expect(within(card).queryByText(/Rules you already know/, { ignore: "[role=tooltip]" })).toBeNull();
  });

  it("is never shown in the Creditor rep view", async () => {
    window.history.replaceState(null, "", "/?view=rep");
    render(<App />);
    await screen.findByRole("button", { name: /Easy deal/ });
    expect(screen.queryByTestId("known-rules")).toBeNull();
    expect(startMsg()).not.toHaveProperty("known_rules");
  });

  it("drops rules the operator entered from the rep's belief until the rep states them", () => {
    const op = { turn: 0, quote: "", source: "operator" as const };
    const ev: BeliefEvent = {
      type: "belief",
      terms: [
        { field: "max_payments", value: 6, status: "KNOWN", evidence: [op], history: [] },
        { field: "min_payment_cents", value: 10000, status: "KNOWN", evidence: [op, { turn: 2, quote: "a hundred" }], history: [] },
        { field: "max_segments", value: 2, status: "ASSUMED", evidence: [], history: [] },
      ],
    };
    const out = toRepView(ev) as BeliefEvent;
    expect(out.terms.map((t) => t.field)).toEqual(["min_payment_cents", "max_segments"]);
  });
});
