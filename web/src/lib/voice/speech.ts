/**
 * Text-to-speech helpers for the voice engine: what to say and with which voice.
 *
 * The chat keeps the agent's symbols ("45%", "$1,250"); only the TTS input is
 * expanded, because Chrome's synthesizer stumbles on `%` and `$`. These never
 * change numbers: they only spell out the symbol next to digits the server
 * already rendered through `Fact.render()`.
 */

/** Natural-sounding voices first; first match wins (exact name, then prefix). */
export const PREFERRED_VOICES = [
  "Google US English",
  "Samantha",
  "Microsoft Aria Online (Natural) - English (United States)",
  "Microsoft Jenny Online (Natural) - English (United States)",
  "Microsoft Aria",
  "Microsoft Jenny",
  "Karen",
  "Daniel",
  "Alex",
] as const;

export interface VoiceLike {
  name: string;
  lang: string;
}

/** "45%" → "45 percent", "$1,250" → "1,250 dollars" (speech only; chat keeps symbols). */
export function speakableText(text: string): string {
  return String(text)
    .replace(/\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)/g, "$1 dollars")
    .replace(/(\d+(?:\.\d+)?)\s*%/g, "$1 percent")
    .replace(/\s+/g, " ")
    .trim();
}

/** Errors our own cancel / barge-in cause; they must not show "playback failed". */
export function isBenignTtsError(err: unknown): boolean {
  const e = String(err ?? "").toLowerCase();
  return e === "interrupted" || e === "canceled" || e === "cancelled";
}

/** Preferred voice, else any en-US voice, else null (browser default). */
export function pickVoice<V extends VoiceLike>(voices: readonly V[]): V | null {
  for (const name of PREFERRED_VOICES) {
    const hit = voices.find((v) => v.name === name) ?? voices.find((v) => v.name.startsWith(name));
    if (hit) return hit;
  }
  return voices.find((v) => /^en[-_]US$/i.test(v.lang)) ?? null;
}
