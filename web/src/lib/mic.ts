/**
 * Mic state machine for the conversation panel (UI only in 23a).
 *
 * off → listening (mic on) → thinking (rep finished, server working) →
 * speaking (agent TTS) → listening. Barge-in while speaking goes straight
 * back to listening. 23b's `useVoice` drives this from VAD and TTS callbacks;
 * fixture mode drives it from replayed server events via `micEventFor`.
 */
import type { ServerEvent } from "@/types/protocol";

export type MicState = "off" | "listening" | "thinking" | "speaking";

export type MicEvent =
  | { type: "toggle" }
  | { type: "rep_done" }
  | { type: "agent_say" }
  | { type: "agent_done" }
  | { type: "barge_in" }
  | { type: "call_over" };

export function micReducer(state: MicState, ev: MicEvent): MicState {
  switch (ev.type) {
    case "toggle":
      return state === "off" ? "listening" : "off";
    case "call_over":
      return "off";
    case "rep_done":
      return state === "off" ? state : "thinking";
    case "agent_say":
      return state === "off" ? state : "speaking";
    case "agent_done":
    case "barge_in":
      return state === "off" ? state : "listening";
  }
}

/** Map a replayed server event to a mic transition, if it implies one. */
export function micEventFor(ev: ServerEvent): MicEvent | null {
  if (ev.type === "transcript" && ev.role === "creditor") return { type: "rep_done" };
  if (ev.type === "say") return { type: "agent_say" };
  // A call ends as a deal or a handoff. Any sign of either ends it for the mic:
  // the handoff event, or an END / ESCALATE phase whether or not it names the move.
  if (ev.type === "escalate") return { type: "call_over" };
  if (ev.type === "phase") {
    if (ev.phase === "END" || ev.phase === "ESCALATE") return { type: "call_over" };
    return ev.intent === null ? { type: "agent_done" } : null;
  }
  return null;
}

export const MIC_LABEL: Record<MicState, string> = {
  off: "Mic off",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};
