/**
 * Display formatting for protocol values. Money arrives as integer cents and
 * percentages as basis points; this is the only place they become strings.
 * Always en-US ("$1,250.00", never "US$").
 */
import type { TermField } from "@/types/protocol";

const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function money(cents: number | null | undefined): string {
  if (cents == null) return "—";
  return USD.format(cents / 100);
}

/** Spoken style, as the agent says it: whole dollars drop the cents ("$75", "$75.50"). */
export function moneyShort(cents: number): string {
  return cents % 100 === 0 ? `$${(cents / 100).toLocaleString("en-US")}` : money(cents);
}

function isTier(v: unknown): v is [number, number] {
  return Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === "number");
}

/** 4500 → "45%", 4250 → "42.5%". */
export function pct(bp: number | null | undefined): string {
  if (bp == null) return "—";
  const v = bp / 100;
  return `${Number.isInteger(v) ? v : v.toFixed(1)}%`;
}

export function isoDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function ms(v: number | null | undefined): string {
  if (v == null) return "—";
  return v < 10 ? `${v.toFixed(1)} ms` : `${Math.round(v).toLocaleString("en-US")} ms`;
}

/** 1 → "1st", 2 → "2nd", 11 → "11th", 22 → "22nd" (matches app.domain.units.render_ordinal). */
export function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  const suffix = { 1: "st", 2: "nd", 3: "rd" }[n % 10] ?? "th";
  return `${n}${suffix}`;
}

export const FIELD_LABEL: Record<TermField, string> = {
  max_payments: "Max payments",
  min_payment_cents: "Minimum payment",
  payment_structure: "Structure",
  first_payment_date: "First payment",
  max_segments: "Max segments",
  max_token_pays: "Max token payments",
  min_payment_tiers: "Payment tiers",
};

export function fieldLabel(field: string): string {
  return (FIELD_LABEL as Record<string, string>)[field] ?? field;
}

/**
 * Render a belief/NLU value for its field ("$100.00", "Nov 2, 2026", "No special tiers").
 * Tiers use the spoken money style ("$75 from the 4th payment"), matching the agent's line.
 * `unknown` because a dropped NLU term can carry anything the LLM produced.
 */
export function termValue(field: string, value: unknown): string {
  if (value == null) return "—";
  if (field === "min_payment_cents" && typeof value === "number") return money(value);
  if (field === "first_payment_date" && typeof value === "string") return isoDate(value);
  // Structure codes (even / balloon / flexible) read as words in sentence case.
  if (field === "payment_structure" && typeof value === "string" && value) return value[0]!.toUpperCase() + value.slice(1);
  if (field === "min_payment_tiers" && Array.isArray(value) && value.every(isTier)) {
    if (value.length === 0) return "No special tiers";
    return value.map(([from, cents]) => `${moneyShort(cents)} from the ${ordinal(from)} payment`).join(" and ");
  }
  if (Array.isArray(value)) return JSON.stringify(value);
  return String(value);
}
