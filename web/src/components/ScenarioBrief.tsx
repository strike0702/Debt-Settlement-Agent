/**
 * Operator scenario brief (`GET /scenarios/{id}`): who the creditor is, the
 * client's savings plan, and the firm's fees.
 *
 * Operator lens only. App does not fetch the brief in the creditor's eye, and
 * this component renders a lock if it is ever handed one there anyway.
 */
import { PrivateLock, PrivateTag } from "@/components/PrivateLock";
import { Card, CardBody } from "@/components/ui/card";
import { isoDate, money, pct } from "@/lib/format";
import type { Lens } from "@/lib/lens";
import type { ScenarioBrief as Brief } from "@/types/protocol";

function Row({ label, value, priv }: { label: string; value: string; priv?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-t border-border py-1.5 first:border-t-0">
      <dt className="text-muted">{label}</dt>
      <dd className="m-0 flex items-center gap-2 text-right num">
        {value}
        {priv && <PrivateTag />}
      </dd>
    </div>
  );
}

export function ScenarioBrief({ brief, lens }: { brief: Brief; lens: Lens }) {
  if (lens !== "operator") return <PrivateLock what="The scenario brief" />;
  const { creditor, client, firm } = brief;
  return (
    <Card>
      <details open>
        <summary className="flex cursor-pointer items-center justify-between px-4 py-4 text-base font-semibold">
          Scenario brief
          <span className="text-sm font-normal text-muted">{brief.title}</span>
        </summary>
        <CardBody>
          <dl className="m-0 text-sm">
            <Row label="Creditor" value={creditor.name} />
            <Row label="Balance owed" value={money(creditor.creditor_balance_cents)} />
            <Row label="Original balance" value={money(creditor.original_balance_cents)} />
            <Row label="Client savings now" value={money(client.sda_balance_cents)} priv />
            <Row label="Monthly deposit" value={`${money(client.draft_amount_cents)} on day ${client.draft_day}`} priv />
            <Row
              label="Deposits"
              value={`${client.upcoming_drafts} through ${isoDate(client.last_draft_date)}`}
              priv
            />
            <Row label="Program fee" value={`${pct(firm.program_fee_bp)} (${money(firm.program_fee_cents)})`} priv />
            <Row label="Bank fee per payment" value={money(firm.bank_fee_cents)} priv />
          </dl>
        </CardBody>
      </details>
    </Card>
  );
}
