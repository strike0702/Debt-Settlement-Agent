import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ClientLedger } from "@/components/ClientLedger";
import type { ScenarioBrief } from "@/types/protocol";

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
    first_draft_date: "2026-02-15",
    last_draft_date: "2026-04-15",
    upcoming_drafts: 2,
    upcoming_deposits_cents: 44000,
    upcoming_withdrawals_cents: 0,
    upcoming_ledger: [],
    ledger: [
      { date: "2026-02-15", amount_cents: 22000, type: "credit", scheduled: false },
      { date: "2026-03-15", amount_cents: 22000, type: "credit", scheduled: true },
      { date: "2026-04-15", amount_cents: 22000, type: "credit", scheduled: true },
    ],
  },
  firm: { program_fee_bp: 1800, program_fee_cents: 28800, bank_fee_cents: 950 },
};

describe("ClientLedger (Phase 35)", () => {
  it("renders the ledger table with running balance, past vs scheduled", () => {
    render(<ClientLedger brief={BRIEF} lens="operator" />);
    expect(screen.getByText("Client deposits and credits")).toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Date", "Description", "Credit", "Debit", "Running balance"]);
    const rows = screen.getAllByRole("row").slice(1);
    const text = rows.map((r) => r.textContent);
    // Past group, the as-of balance, then the scheduled group.
    expect(text[0]).toBe("Past");
    expect(text[1]).toContain("Feb 15, 2026");
    expect(text[1]).toContain("Monthly deposit");
    expect(rows[1]).toHaveAttribute("data-scheduled", "false");
    expect(text[2]).toContain("Balance today");
    expect(text[2]).toContain("$440.00");
    expect(text[3]).toBe("Scheduled");
    expect(text[4]).toContain("$660.00");
    expect(rows[4]).toHaveAttribute("data-scheduled", "true");
    expect(text[5]).toContain("$880.00");
    expect(within(rows[4]!).getByText("$220.00")).toBeInTheDocument();
  });

  it("renders only a lock in the creditor rep lens", () => {
    render(<ClientLedger brief={BRIEF} lens="creditor" />);
    expect(screen.getByTestId("private-lock")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("$220.00");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
