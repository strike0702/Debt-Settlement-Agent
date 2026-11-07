/**
 * The "Known creditor rules" panel's form: what the operator typed (strings) ↔ the
 * `start.known_rules` wire object (cents ints, ISO date, tiers as
 * `{from_payment, min_cents}`). Every field is optional; an empty one is "not known".
 *
 * The checks mirror the server (`app.domain.known_rules.parse_known_rules`) so a bad
 * entry is shown at the field before Start; the server still validates and refuses
 * the call with the same kind of message. Pure; no React.
 */
import { formatCushion, parseDollars } from "@/lib/cushion";
import type { KnownRules } from "@/types/protocol";

export const MAX_PAYMENTS = 60;
export const MAX_SEGMENTS = 10;

export type Structure = "" | "even" | "balloon" | "flexible";
export interface TierRow {
  /** Stable React key for the row. */
  key: number;
  from: string;
  min: string;
}
export interface RulesForm {
  max_payments: string;
  min_payment_cents: string;
  payment_structure: Structure;
  min_payment_tiers: TierRow[];
  max_segments: string;
  max_token_pays: string;
  first_payment_date: string;
}
export type RuleKey = keyof RulesForm;
export type RuleErrors = Partial<Record<RuleKey, string>>;

export const EMPTY_FORM: RulesForm = {
  max_payments: "",
  min_payment_cents: "",
  payment_structure: "",
  min_payment_tiers: [],
  max_segments: "",
  max_token_pays: "",
  first_payment_date: "",
};

let nextKey = 1;
/** A new tier row (fresh key). */
export function tierRow(from = "", min = ""): TierRow {
  return { key: nextKey++, from, min };
}

const WHOLE = /^\d+$/;

function count(text: string, lo: number, hi: number): number | null {
  const t = text.trim();
  if (!WHOLE.test(t)) return null;
  const n = Number(t);
  return n >= lo && n <= hi ? n : null;
}

/** The wire rules for `form`, and an error sentence per field that does not parse or fit. */
export function parseRules(form: RulesForm, asOf?: string): { rules: KnownRules; errors: RuleErrors } {
  const rules: KnownRules = {};
  const errors: RuleErrors = {};
  const filled = (k: RuleKey) => typeof form[k] === "string" && (form[k] as string).trim() !== "";

  if (filled("max_payments")) {
    const n = count(form.max_payments, 1, MAX_PAYMENTS);
    if (n === null) errors.max_payments = `Write a whole number from 1 to ${MAX_PAYMENTS}.`;
    else rules.max_payments = n;
  }
  if (filled("min_payment_cents")) {
    const c = parseDollars(form.min_payment_cents);
    if (c === null || c <= 0) errors.min_payment_cents = "Write a dollar amount, for example 100 or 100.50.";
    else rules.min_payment_cents = c;
  }
  if (form.payment_structure) rules.payment_structure = form.payment_structure;

  const rows = form.min_payment_tiers.filter((r) => r.from.trim() !== "" || r.min.trim() !== "");
  if (rows.length > 0) {
    const tiers: { from_payment: number; min_cents: number }[] = [];
    for (const [i, r] of rows.entries()) {
      const from = count(r.from, 1, MAX_PAYMENTS);
      const min = parseDollars(r.min);
      if (from === null || min === null || min <= 0) {
        errors.min_payment_tiers = `Tier ${i + 1} needs a payment number (1 to ${MAX_PAYMENTS}) and a dollar amount.`;
        break;
      }
      tiers.push({ from_payment: from, min_cents: min });
    }
    if (!errors.min_payment_tiers) {
      const last = tiers.at(-1)!;
      if (tiers.some((t, i) => i > 0 && (t.from_payment <= tiers[i - 1]!.from_payment || t.min_cents <= tiers[i - 1]!.min_cents)))
        errors.min_payment_tiers = "Each tier must start at a later payment and ask for more than the one before.";
      else if (rules.max_payments != null && last.from_payment > rules.max_payments)
        errors.min_payment_tiers = `A tier starts at payment ${last.from_payment}, after the last allowed payment (${rules.max_payments}).`;
      else if (rules.min_payment_cents != null && tiers[0]!.min_cents < rules.min_payment_cents)
        errors.min_payment_tiers = "No tier can ask for less than the minimum payment.";
      else rules.min_payment_tiers = tiers;
    }
  }
  if (filled("max_segments")) {
    const n = count(form.max_segments, 1, MAX_SEGMENTS);
    if (n === null) errors.max_segments = `Write a whole number from 1 to ${MAX_SEGMENTS}.`;
    else rules.max_segments = n;
  }
  if (filled("max_token_pays")) {
    const n = count(form.max_token_pays, 0, MAX_PAYMENTS);
    if (n === null) errors.max_token_pays = `Write a whole number from 0 to ${MAX_PAYMENTS}.`;
    else if (rules.max_payments != null && n > rules.max_payments)
      errors.max_token_pays = "Token payments cannot be more than max payments.";
    else rules.max_token_pays = n;
  }
  if (filled("first_payment_date")) {
    const d = form.first_payment_date.trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d) || Number.isNaN(Date.parse(d))) errors.first_payment_date = "Pick a date.";
    else if (asOf && d < asOf) errors.first_payment_date = "The first payment cannot be before today's date for this call.";
    else rules.first_payment_date = d;
  }
  return { rules, errors };
}

/** The form for wire `rules` (a firm default or the scenario's true rules). */
export function formFromRules(rules: KnownRules | null | undefined): RulesForm {
  const r = rules ?? {};
  const num = (v: number | null | undefined) => (v == null ? "" : String(v));
  return {
    max_payments: num(r.max_payments),
    min_payment_cents: r.min_payment_cents == null ? "" : formatCushion(r.min_payment_cents),
    payment_structure: r.payment_structure ?? "",
    min_payment_tiers: (r.min_payment_tiers ?? []).map((t) => tierRow(String(t.from_payment), formatCushion(t.min_cents))),
    max_segments: num(r.max_segments),
    max_token_pays: num(r.max_token_pays),
    first_payment_date: r.first_payment_date ?? "",
  };
}

/** Same rules (absent and null are the same; field order does not matter). */
export function sameRules(a: KnownRules | null | undefined, b: KnownRules | null | undefined): boolean {
  const norm = (r: KnownRules | null | undefined) =>
    JSON.stringify(
      Object.entries(r ?? {})
        .filter(([, v]) => v != null && !(Array.isArray(v) && v.length === 0))
        .sort(([x], [y]) => x.localeCompare(y)),
    );
  return norm(a) === norm(b);
}
