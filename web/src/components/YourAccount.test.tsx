/**
 * "Your account" card (Phase 34) over a mocked `fetch`: it calls the rep-safe
 * `/scenarios/{id}/rep`, renders the creditor's balances and rules, and shows
 * loading and error states.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ruleLines, YourAccount } from "@/components/YourAccount";
import type { RepAccount } from "@/types/protocol";

const EASY_DEAL: RepAccount = {
  id: "easy_deal",
  creditor: { name: "NorthPeak Collections", outstanding_balance_cents: 125000, original_balance_cents: 160000 },
  rules: {
    max_payments: 8,
    min_payment_cents: 10000,
    structure: "even",
    opening_ask_bp: 4500,
    floor_bp: 4000,
    first_payment: null,
  },
};

function mockFetch(respond: (url: string) => Response | Promise<Response>) {
  const fn = vi.fn(async (url: string) => respond(url));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("YourAccount", () => {
  it("fetches the rep endpoint and shows the account and rules", async () => {
    const fetchMock = mockFetch(() => new Response(JSON.stringify(EASY_DEAL), { status: 200 }));
    render(<YourAccount source={{ kind: "catalog", id: "easy_deal" }} />);
    expect(screen.getByText("Loading your account…")).toBeInTheDocument();
    expect(await screen.findByText("NorthPeak Collections")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/scenarios/easy_deal/rep");
    expect(fetchMock).not.toHaveBeenCalledWith("/scenarios/easy_deal");
    expect(screen.getByText("$1,250.00")).toBeInTheDocument();
    expect(screen.getByText("$1,600.00")).toBeInTheDocument();
    for (const line of [
      "Up to 8 payments",
      "At least $100 each",
      "Even payments",
      "Opening ask 45%",
      "Floor 40% — don't go below",
    ]) {
      expect(screen.getByText(line)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("private-lock")).not.toBeInTheDocument();
  });

  it("shows an error when the endpoint fails", async () => {
    mockFetch(() => new Response("{}", { status: 404 }));
    render(<YourAccount source={{ kind: "catalog", id: "nope" }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your account (Error: HTTP 404)");
  });

  it("posts a custom case to the rep-safe preview endpoint (Phase 36)", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify({ ...EASY_DEAL, id: "custom", suggested: [] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const payload = { offer: { creditor: "NorthPeak Collections" }, client: { draft_amount_cents: 1 } };
    render(<YourAccount source={{ kind: "custom", key: "custom:1", version: 1, payload }} />);
    expect(await screen.findByText("Up to 8 payments")).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/scenarios/preview/rep");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual(payload);
  });

  it("leaves out rules the card does not state and keeps free-text ones", () => {
    const lines = ruleLines({
      max_payments: 1,
      min_payment_cents: 2050,
      structure: "balloon",
      opening_ask_bp: 4250,
      floor_bp: null,
      first_payment: "after the last savings draft",
    });
    expect(lines).toEqual([
      "A single payment",
      "At least $20.50 each",
      "Balloon: a larger last payment is fine",
      "Opening ask 42.5%",
      "First payment: after the last savings draft",
    ]);
  });
});
