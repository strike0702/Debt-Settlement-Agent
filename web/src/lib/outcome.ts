/**
 * How a call ended, in words, for both views (Phase 48).
 *
 * Since Phase 45 a call ends only as a deal or a handoff to a specialist.
 * `callOutcome` reads the folded call state and says which, for the state
 * column's outcome card and the end-of-call line under the transcript.
 *
 * Privacy: the reason sentence (from the server's `reasons.py`, via the
 * ESCALATE turn's trace) is Debt negotiator only; several reasons name the
 * client's limit or savings. The Creditor rep view gets only what the agent
 * said to the rep (`escalate.escalate_reason`), which they already heard.
 * Pure; no React.
 */
import type { CallState } from "@/lib/callState";
import { isCallOver } from "@/lib/callState";
import type { Lens } from "@/lib/lens";
import { HANDOFF_TITLE } from "@/lib/traceStory";

export interface CallOutcome {
  kind: "deal" | "handoff";
  title: string;
  /** Why the agent handed off (Debt negotiator view only; null in the rep view or when unknown). */
  reason: string | null;
  /** What the agent told the rep when it handed off (public: the rep heard it). */
  said: string | null;
}

/** The deal or handoff this call reached, or null while it is still open (or ended with neither). */
export function callOutcome(state: CallState, lens: Lens): CallOutcome | null {
  if (state.phase === "ESCALATE" || state.escalation != null) {
    const turn = lens === "operator" ? state.traces.filter((t) => t.decide.intent === "ESCALATE").at(-1) : undefined;
    return {
      kind: "handoff",
      title: HANDOFF_TITLE,
      reason: turn?.decide.reason_text ?? null,
      said: state.escalation?.escalate_reason ?? null,
    };
  }
  if (state.agreement) return { kind: "deal", title: "Agreement drafted", reason: null, said: null };
  return null;
}

/** The quiet line under the transcript once the call is over (null while it runs). */
export function endedLine(state: CallState, lens: Lens): string | null {
  if (!isCallOver(state) && state.autoplay == null) return null;
  const outcome = callOutcome(state, lens);
  if (outcome?.kind === "handoff") return "Call ended: handed off to a specialist";
  if (outcome?.kind === "deal") return "Call ended: deal sent to the client for approval";
  return "Call ended";
}
