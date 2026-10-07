/**
 * "Your account": the creditor's own side of the call, for the human playing
 * the rep (`GET /scenarios/{id}/rep`).
 *
 * Creditor lens only; it takes the slot the operator's ScenarioBrief uses. It
 * shows the creditor's balances and their settlement rules from the rep card,
 * never the client's finances or the firm's fees (those stay in the brief).
 */
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { useRepAccount } from "@/hooks/useScenarios";
import { money, moneyShort, pct } from "@/lib/format";
import type { RepAccount } from "@/types/protocol";

const STRUCTURE: Record<NonNullable<RepAccount["rules"]["structure"]>, string> = {
  even: "Even payments",
  balloon: "Balloon: a larger last payment is fine",
  flexible: "Flexible payment amounts",
};

/** The rep's rules as short lines, in rep-card order; rules the card does not state are left out. */
export function ruleLines(rules: RepAccount["rules"]): string[] {
  const out: string[] = [];
  const n = rules.max_payments;
  if (n != null) out.push(n === 1 ? "A single payment" : `Up to ${n} payments`);
  if (rules.min_payment_cents != null) out.push(`At least ${moneyShort(rules.min_payment_cents)} each`);
  if (rules.structure) out.push(STRUCTURE[rules.structure]);
  if (rules.opening_ask_bp != null) out.push(`Opening ask ${pct(rules.opening_ask_bp)}`);
  if (rules.floor_bp != null) out.push(`Floor ${pct(rules.floor_bp)} — don't go below`);
  if (rules.first_payment) out.push(`First payment: ${rules.first_payment}`);
  return out;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-t border-border py-1.5 first:border-t-0">
      <dt className="text-muted">{label}</dt>
      <dd className="m-0 text-right num">{value}</dd>
    </div>
  );
}

export function YourAccount({ scenarioId }: { scenarioId: string }) {
  const state = useRepAccount(scenarioId);
  return (
    <Card aria-label="Your account">
      <CardHeader title="Your account" aside={<span className="text-sm text-muted">You are the creditor</span>} />
      <CardBody>
        {state.status === "loading" && <p className="m-0 text-sm text-muted">Loading your account…</p>}
        {state.status === "error" && (
          <p role="alert" className="m-0 text-sm text-muted">
            Could not load your account ({state.message}).
          </p>
        )}
        {state.status === "ready" && <AccountBody account={state.account} />}
      </CardBody>
    </Card>
  );
}

function AccountBody({ account }: { account: RepAccount }) {
  const { creditor, rules } = account;
  const lines = ruleLines(rules);
  return (
    <>
      <dl className="m-0 text-sm">
        <Row label="Creditor" value={creditor.name ?? "—"} />
        <Row label="Outstanding balance" value={money(creditor.outstanding_balance_cents)} />
        <Row label="Original balance" value={money(creditor.original_balance_cents)} />
      </dl>
      <h3 className="mt-3 mb-1 text-sm font-semibold">Your settlement rules</h3>
      {lines.length > 0 ? (
        <ul className="m-0 list-disc pl-5 text-sm">
          {lines.map((l) => (
            <li key={l} className="py-0.5">
              {l}
            </li>
          ))}
        </ul>
      ) : (
        <p className="m-0 text-sm text-muted">No rules on your card for this case.</p>
      )}
    </>
  );
}
