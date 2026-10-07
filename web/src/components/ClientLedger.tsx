/**
 * Debt negotiator view: the client's dedicated-account ledger (deposits and
 * withdrawals) with a running balance, from the operator brief
 * (`GET /scenarios/{id}`, `client.ledger`).
 *
 * PRIVATE. App renders it only in the Debt negotiator view, and it renders a
 * lock if it is ever handed the Creditor rep lens. Rows on or before the
 * as-of date are past (already in the balance); later rows are scheduled.
 * Each group gets a label row ("Past" / "Scheduled") instead of per-row badges,
 * so the five columns fit the narrow state column.
 */
import { PrivateLock, PrivateTag } from "@/components/PrivateLock";
import { Card, CardBody } from "@/components/ui/card";
import { isoDate, money } from "@/lib/format";
import { ledgerRows } from "@/lib/ledger";
import type { Lens } from "@/lib/lens";
import type { ScenarioBrief } from "@/types/protocol";

export function ClientLedger({ brief, lens }: { brief: ScenarioBrief; lens: Lens }) {
  if (lens !== "operator") return <PrivateLock what="The client's ledger" />;
  const rows = ledgerRows(brief.client);
  const past = rows.filter((r) => !r.scheduled);
  const scheduled = rows.filter((r) => r.scheduled);
  return (
    <Card>
      <details open>
        <summary className="flex cursor-pointer items-center justify-between gap-3 px-4 py-4 text-base font-semibold">
          Client deposits and credits
          <PrivateTag />
        </summary>
        <CardBody>
          {rows.length === 0 ? (
            <p className="text-sm text-muted">No ledger entries.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm num">
                <caption className="sr-only">Client dedicated-account ledger with running balance</caption>
                <thead className="text-left text-muted">
                  <tr className="align-bottom">
                    <th className="py-1.5 pr-2 pl-1 font-medium">Date</th>
                    <th className="py-1.5 pr-2 font-medium">Description</th>
                    <th className="py-1.5 pr-2 text-right font-medium">Credit</th>
                    <th className="py-1.5 pr-2 text-right font-medium">Debit</th>
                    <th className="py-1.5 pr-1 text-right font-medium">Running balance</th>
                  </tr>
                </thead>
                <tbody>
                  {past.length > 0 && <Section label="Past" />}
                  {past.map((r, i) => (
                    <LedgerLine key={`p-${i}`} row={r} />
                  ))}
                  <tr className="border-t border-border bg-surface-2 align-top" data-testid="ledger-as-of">
                    <td className="py-1.5 pr-2 pl-1 whitespace-nowrap">{isoDate(brief.client.as_of_date)}</td>
                    <td className="py-1.5 pr-2 font-medium" colSpan={3}>
                      Balance today (as of)
                    </td>
                    <td className="py-1.5 pr-1 text-right font-medium">{money(brief.client.sda_balance_cents)}</td>
                  </tr>
                  {scheduled.length > 0 && <Section label="Scheduled" />}
                  {scheduled.map((r, i) => (
                    <LedgerLine key={`s-${i}`} row={r} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </details>
    </Card>
  );
}

/** Group label row: past entries sit above the as-of balance, scheduled ones below. */
function Section({ label }: { label: string }) {
  return (
    <tr className="border-t border-border">
      <th scope="rowgroup" colSpan={5} className="pt-2.5 pb-1 pl-1 text-left text-xs font-medium tracking-wide text-muted uppercase">
        {label}
      </th>
    </tr>
  );
}

function LedgerLine({ row }: { row: ReturnType<typeof ledgerRows>[number] }) {
  return (
    <tr className="border-t border-border align-top" data-scheduled={row.scheduled}>
      <td className="py-1.5 pr-2 pl-1 whitespace-nowrap">{isoDate(row.date)}</td>
      <td className="py-1.5 pr-2">{row.description}</td>
      <td className="py-1.5 pr-2 text-right">{row.credit == null ? "" : money(row.credit)}</td>
      <td className="py-1.5 pr-2 text-right">{row.debit == null ? "" : money(row.debit)}</td>
      <td className="py-1.5 pr-1 text-right">{money(row.balance)}</td>
    </tr>
  );
}
