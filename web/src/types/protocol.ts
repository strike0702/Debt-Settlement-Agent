/**
 * Hand-written helpers on top of the generated protocol types (events.ts).
 *
 * events.ts is generated from the Pydantic models and must not be edited; the
 * aliases here name the narrower shapes the UI relies on (term fields, tier
 * pairs, stances) and the HTTP payloads that are not WS events (`/scenarios`,
 * `/scenarios/{id}`, `/scenarios/{id}/rep`, `/scenarios/{id}/rep_card`).
 */
import type { BeliefTerm, TurnTraceEvent } from "./events";

export type * from "./events";

export type TermField =
  | "max_payments"
  | "min_payment_cents"
  | "payment_structure"
  | "first_payment_date"
  | "max_segments"
  | "max_token_pays"
  | "min_payment_tiers";

export type TermStatus = BeliefTerm["status"];
export type Stance = NonNullable<TurnTraceEvent["stance"]>;
/** A belief/NLU value on the wire; tiers are `[[from_payment, min_cents], ...]`. */
export type TermValue = BeliefTerm["value"];
export type TraceTimings = TurnTraceEvent["timings"];

// ---------------------------------------------------------------- HTTP

/** One row of `GET /scenarios`. `suggested` = the rep card's suggested replies, in call order. */
export interface ScenarioMeta {
  id: string;
  title: string;
  description: string;
  expected: "deal" | "no_deal" | "escalate" | "counter" | string;
  suggested: string[];
}

export interface LedgerEntry {
  date: string;
  amount_cents: number;
  type: "credit" | "debit" | string;
}

/** `GET /scenarios/{id}`: the operator brief. `client` and `firm` are PRIVATE (operator lens only). */
export interface ScenarioBrief extends Omit<ScenarioMeta, "suggested"> {
  creditor: { name: string; creditor_balance_cents: number; original_balance_cents: number };
  client: {
    as_of_date: string;
    sda_balance_cents: number;
    draft_amount_cents: number;
    draft_day: number;
    first_draft_date: string;
    last_draft_date: string;
    upcoming_drafts: number;
    upcoming_deposits_cents: number;
    upcoming_withdrawals_cents: number;
    upcoming_ledger: LedgerEntry[];
  };
  firm: { program_fee_bp: number; program_fee_cents: number; bank_fee_cents: number };
}

/**
 * `GET /scenarios/{id}/rep`: the creditor's own account and settlement rules, parsed
 * from the rep card. Rep-safe (no client or firm data). A rule the card does not state,
 * or states in a form the server cannot parse, is `null`.
 */
export interface RepAccount {
  id: string;
  creditor: { name: string | null; outstanding_balance_cents: number | null; original_balance_cents: number | null };
  rules: {
    max_payments: number | null;
    min_payment_cents: number | null;
    structure: "even" | "balloon" | "flexible" | null;
    opening_ask_bp: number | null;
    floor_bp: number | null;
    first_payment: string | null;
  };
}
