/**
 * Every protocol code the console shows as words, in sentence case (Phase 36).
 *
 * The server speaks in codes (`COUNTER`, `KNOWN`, guard stage `rendered`,
 * expected `no_deal`); a newcomer watching the demo should read words. This is
 * the one place those codes become labels, so the rule ("capital first letter,
 * the rest as in a sentence") is applied once. The raw codes still appear,
 * deliberately, in monospace where the trace shows the policy's reason code.
 */
import type { TermStatus } from "@/types/protocol";

/** "pending client approval" → "Pending client approval"; leaves the rest alone. */
export function sentence(text: string): string {
  const t = text.trim();
  return t ? t[0]!.toUpperCase() + t.slice(1) : t;
}

/** How sure the agent is about a term it heard. */
export const STATUS_LABEL: Record<TermStatus, string> = {
  KNOWN: "Confirmed",
  TENTATIVE: "Tentative",
  CONTRADICTED: "Contradicted",
  ASSUMED: "Default",
  UNKNOWN: "Unknown",
};

/** A scenario's expected outcome, for its card. */
export const EXPECTED_LABEL: Record<string, string> = {
  deal: "Deal",
  counter: "Counters, then deal",
  no_deal: "No deal",
  escalate: "Hands off to a person",
};

export function expectedLabel(expected: string): string {
  return EXPECTED_LABEL[expected] ?? sentence(expected.replaceAll("_", " "));
}
