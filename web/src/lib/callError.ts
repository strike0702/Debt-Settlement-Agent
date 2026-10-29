/**
 * Plain copy for the call's `error` frames (server and voice client).
 *
 * `callState` keeps only the newest one (`CallState.error`); the conversation
 * panel shows `callErrorText` next to the composer in both views and the raw
 * server message in the Debt negotiator view only (it can name providers).
 */

export interface CallError {
  kind: "error";
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
  // WebRTC voice: client-side failures and /voice/offer refusals.
  [/^mic blocked/i, "The browser blocked the mic. Allow Microphone from the site icon in the address bar, then turn voice on again."],
  [/^mic unavailable/i, "The mic could not start. Check that one is connected, then turn voice on again."],
  [/^voice connection failed/i, "Voice could not connect. Check your network, then turn voice on again. You can still type."],
  [/^Voice is paused/i, "Voice is paused for today because the voice budget is used up. You can still type."],
  [/^Voice is not configured/i, "Voice is not set up on this server. You can still type."],
  [/already has a voice session/i, "Voice is already on for this call in another tab."],
  [/^Start the call before turning voice on/i, "The call has not started yet. Press Start call first."],
];

// Voice failures (browser side and /voice/offer refusals): over once the link is live.
const VOICE = /^(mic blocked|mic unavailable|voice connection failed|Voice is |Start the call before turning voice on)|already has a voice session/i;
// Raised in this browser, not sent by the server.
const CLIENT = /^(mic blocked|mic unavailable|voice connection failed)/i;

/** A voice error, cleared once the voice link is live again. */
export function isVoiceError(message: string): boolean {
  return VOICE.test(message);
}

/** Label for the raw detail under the error line (operator view). */
export function errorDetailLabel(message: string): string {
  return CLIENT.test(message) ? "Browser detail" : "Server message";
}

export const GENERIC_ERROR = "Something went wrong on the server. Try again.";

/** One short, sentence-case line for a call error. */
export function callErrorText(e: Pick<CallError, "kind" | "message">): string {
  return KNOWN.find(([re]) => re.test(e.message))?.[1] ?? GENERIC_ERROR;
}
