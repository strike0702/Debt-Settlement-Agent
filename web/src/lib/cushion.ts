/**
 * The minimum client cushion field's money text: parse what the operator typed into
 * whole cents and format the firm default back. Same bounds as the server
 * (`app.domain.scenario.parse_min_cushion`): 0 to $5,000. `parseDollars` is shared with
 * the known creditor rules panel. Pure; no React.
 */
export const MAX_CUSHION_CENTS = 500_000;

const MONEY = /^\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?$/;

/** Whole cents for a typed dollar amount ("1,200", "$100.5"), or null when it is not one. */
export function parseDollars(text: string): number | null {
  const m = MONEY.exec(text.trim());
  if (!m) return null;
  const dollars = Number(m[1]!.replace(/,/g, ""));
  return dollars * 100 + Number((m[2] ?? "").padEnd(2, "0") || "0");
}

/** Cents for `text`, or an error sentence. Empty text means no cushion (0). */
export function parseCushion(text: string): { cents: number } | { error: string } {
  if (text.trim() === "") return { cents: 0 };
  const cents = parseDollars(text);
  if (cents === null) return { error: "Write a dollar amount, for example 100 or 100.50." };
  if (cents > MAX_CUSHION_CENTS) return { error: "Use $5,000 or less." };
  return { cents };
}

const DOLLARS = new Intl.NumberFormat("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });

/** The field's text for a cents amount ("100", "100.5" → "100.50"). */
export function formatCushion(cents: number): string {
  if (cents % 100 === 0) return DOLLARS.format(cents / 100).replace(/,/g, "");
  return (cents / 100).toFixed(2);
}
