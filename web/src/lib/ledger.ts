/**
 * The client's dedicated-account ledger as table rows for the Debt negotiator
 * view (`ClientLedger`). PRIVATE: built only from the operator brief.
 *
 * The anchor is the balance at `as_of_date` (`sda_balance_cents`). Scheduled
 * entries (after as-of) run forward from it; past entries (on or before as-of,
 * already in that balance) run backward, so each row's balance is the balance
 * just after that entry. Integer cents throughout; formatting is the caller's.
 */
import type { ScenarioBrief } from "@/types/protocol";

export interface LedgerRow {
  date: string;
  description: string;
  credit: number | null;
  debit: number | null;
  /** Balance after this entry, in cents. */
  balance: number;
  scheduled: boolean;
}

function signed(e: { amount_cents: number; type: string }): number {
  return e.type === "debit" ? -e.amount_cents : e.amount_cents;
}

function describe(e: { amount_cents: number; type: string }, draft: number): string {
  if (e.type === "debit") return "Withdrawal";
  return e.amount_cents === draft ? "Monthly deposit" : "Deposit";
}

export function ledgerRows(client: ScenarioBrief["client"]): LedgerRow[] {
  const entries = [...(client.ledger ?? [])].sort((a, b) => a.date.localeCompare(b.date));
  const row = (e: (typeof entries)[number], balance: number): LedgerRow => ({
    date: e.date,
    description: describe(e, client.draft_amount_cents),
    credit: e.type === "debit" ? null : e.amount_cents,
    debit: e.type === "debit" ? e.amount_cents : null,
    balance,
    scheduled: e.scheduled,
  });

  const past = entries.filter((e) => !e.scheduled);
  const pastRows: LedgerRow[] = [];
  let after = client.sda_balance_cents;
  for (let i = past.length - 1; i >= 0; i--) {
    const e = past[i]!;
    pastRows.unshift(row(e, after));
    after -= signed(e);
  }

  let balance = client.sda_balance_cents;
  const scheduledRows = entries
    .filter((e) => e.scheduled)
    .map((e) => {
      balance += signed(e);
      return row(e, balance);
    });
  return [...pastRows, ...scheduledRows];
}
