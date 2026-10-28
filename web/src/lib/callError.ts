/**
 * Plain copy for the server's `error` and `stt_error` frames.
 *
 * `callState` keeps only the newest one (`CallState.error`); the conversation
 * panel shows `callErrorText` next to the composer in both views and the raw
 * server message in the Debt negotiator view only (it can name providers).
 */

export interface CallError {
  kind: "error" | "stt_error";
  /** Raw server text, e.g. "NLU unavailable: all providers failed". */
  message: string;
  /** Counts errors in this call, so a repeat of the same message is still news. */
  seq: number;
}

const KNOWN: readonly [RegExp, string][] = [
  [/^NLU unavailable/i, "The agent could not read that line. Try sending it again."],
  [/^call not started/i, "The call has not started yet. Press Start call first."],
  [/^autoplay is driving this call/i, "The simulated rep is playing this call, so your reply was not sent."],
  [/^server not configured/i, "The server is not set up for calls right now."],
];

export const GENERIC_ERROR = "Something went wrong on the server. Try again.";
export const STT_ERROR = "The agent could not hear that line. Say it again, or type it.";

/** One short, sentence-case line for a call error. */
export function callErrorText(e: Pick<CallError, "kind" | "message">): string {
  if (e.kind === "stt_error") return STT_ERROR;
  return KNOWN.find(([re]) => re.test(e.message))?.[1] ?? GENERIC_ERROR;
}
