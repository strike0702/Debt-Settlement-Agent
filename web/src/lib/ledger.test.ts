import { describe, expect, it } from "vitest";
import { ledgerRows } from "@/lib/ledger";
import type { ScenarioBrief } from "@/types/protocol";

function client(over: Partial<ScenarioBrief["client"]>): ScenarioBrief["client"] {
  return {
    as_of_date: "2026-03-01",
    sda_balance_cents: 44000,
    draft_amount_cents: 22000,
    draft_day: 15,
    first_draft_date: "2026-01-15",
    last_draft_date: "2026-05-15",
    upcoming_drafts: 0,
    upcoming_deposits_cents: 0,
    upcoming_withdrawals_cents: 0,
    upcoming_ledger: [],
    ledger: [],
    ...over,
  };
}

describe("ledgerRows (Phase 35)", () => {
  it("runs scheduled entries forward from the as-of balance", () => {
    const rows = ledgerRows(
      client({
        ledger: [
          { date: "2026-04-15", amount_cents: 22000, type: "credit", scheduled: true },
          { date: "2026-03-15", amount_cents: 22000, type: "credit", scheduled: true },
          { date: "2026-04-20", amount_cents: 5000, type: "debit", scheduled: true },
          { date: "2026-05-15", amount_cents: 10000, type: "credit", scheduled: true },
        ],
      }),
    );
    expect(rows).toEqual([
      { date: "2026-03-15", description: "Monthly deposit", credit: 22000, debit: null, balance: 66000, scheduled: true },
      { date: "2026-04-15", description: "Monthly deposit", credit: 22000, debit: null, balance: 88000, scheduled: true },
      { date: "2026-04-20", description: "Withdrawal", credit: null, debit: 5000, balance: 83000, scheduled: true },
      { date: "2026-05-15", description: "Deposit", credit: 10000, debit: null, balance: 93000, scheduled: true },
    ]);
  });

  it("runs past entries backward so the last past row equals the as-of balance", () => {
    const rows = ledgerRows(
      client({
        ledger: [
          { date: "2026-01-15", amount_cents: 22000, type: "credit", scheduled: false },
          { date: "2026-02-15", amount_cents: 22000, type: "credit", scheduled: false },
          { date: "2026-03-15", amount_cents: 22000, type: "credit", scheduled: true },
        ],
      }),
    );
    expect(rows.map((r) => [r.date, r.balance, r.scheduled])).toEqual([
      ["2026-01-15", 22000, false],
      ["2026-02-15", 44000, false],
      ["2026-03-15", 66000, true],
    ]);
  });

  it("is empty without a ledger", () => {
    expect(ledgerRows(client({ ledger: [] }))).toEqual([]);
  });
});
