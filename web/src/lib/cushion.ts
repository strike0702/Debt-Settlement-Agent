/**
 * The minimum client cushion field's money text: parse what the operator typed into
 * whole cents and format the firm default back. Same bounds as the server
 * (`app.domain.scenario.parse_min_cushion`): 0 to $5,000. Pure; no React.
 */
export const MAX_CUSHION_CENTS = 500_000;

const MONEY = /^\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?$/;

/** Cents for `text`, or an error sentence. Empty text means no cushion (0). */
export function parseCushion(text: string): { cents: number } | { error: string } {
  const t = text.trim();
  if (t === "") return { cents: 0 };
  const m = MONEY.exec(t);
  if (!m) return { error: "Write a dollar amount, for example 100 or 100.50." };
  const dollars = Number(m[1]!.replace(/,/g, ""));
  const cents = dollars * 100 + Number((m[2] ?? "").padEnd(2, "0") || "0");
  if (cents > MAX_CUSHION_CENTS) return { error: "Use $5,000 or less." };
  return { cents };
}

const DOLLARS = new Intl.NumberFormat("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });

/** The field's text for a cents amount ("100", "100.5" → "100.50"). */
export function formatCushion(cents: number): string {
  if (cents % 100 === 0) return DOLLARS.format(cents / 100).replace(/,/g, "");
  return (cents / 100).toFixed(2);
}
