/**
 * Every protocol code the console shows as words, in sentence case (Phase 36).
 *
 * The server speaks in codes (`COUNTER`, `KNOWN`, guard stage `rendered`,
 * expected `no_deal`); a newcomer watching the demo should read words. This is
 * the one place those codes become labels, so the rule ("capital first letter,
 * the rest as in a sentence") is applied once. The raw codes still appear,
 * deliberately, in monospace where the trace shows the policy's reason code.
 */
import type { Intent, TermStatus } from "@/types/protocol";

/** "pending client approval" → "Pending client approval"; leaves the rest alone. */
export function sentence(text: string): string {
  const t = text.trim();
  return t ? t[0]!.toUpperCase() + t.slice(1) : t;
}

/** What the agent did this turn, as a short phrase (the turn card's tag). */
export const INTENT_LABEL: Record<Intent, string> = {
  OPENING: "Opens the call",
  ASK: "Asks for a term",
  ASK_SETTLEMENT: "Asks for their offer",
  READ_BACK: "Reads a term back",
  CLARIFY: "Asks to clarify",
  REFUSE_PRIVATE: "Declines a private question",
  REFUSE_COMMIT: "Declines to commit",
  COUNTER: "Counteroffer",
  COUNTER_TERMS: "Counters on terms",
  CONFIRM_SCHEDULE: "Confirms the schedule",
  SPEAK_SCHEDULE: "Reads the schedule",
  PROPOSE_WRAP: "Proposes to wrap up",
  CLOSE: "Closes the deal",
  NO_DEAL_WRAP: "Ends without a deal",
  ESCALATE: "Hands off to a person",
  ANSWER: "Answers a question",
};

export function intentLabel(intent: string): string {
  return (INTENT_LABEL as Record<string, string>)[intent] ?? sentence(intent.toLowerCase().replaceAll("_", " "));
}

/** How sure the agent is about a term it heard. */
export const STATUS_LABEL: Record<TermStatus, string> = {
  KNOWN: "Confirmed",
  TENTATIVE: "Tentative",
  CONTRADICTED: "Contradicted",
  ASSUMED: "Default",
  UNKNOWN: "Unknown",
};

/** The three checks a phrased line passes before it is spoken. */
export const GUARD_STAGE_LABEL: Record<string, string> = {
  template: "Template check",
  unfilled: "Placeholder check",
  rendered: "Final line check",
};

export function guardLabel(stage: string, ok: boolean, reason: string | null | undefined): string {
  const name = GUARD_STAGE_LABEL[stage] ?? sentence(stage);
  return ok ? `${name} passed` : `${name} blocked: ${(reason ?? "").replaceAll("_", " ")}`;
}

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
