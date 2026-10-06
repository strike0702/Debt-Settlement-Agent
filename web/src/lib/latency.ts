/**
 * Latency waterfall rows for one turn: each pipeline stage as a bar that
 * starts where the previous one ended. Stages missing from the trace are
 * skipped, so offline template turns just show fewer bars.
 */
import type { TraceTimings } from "@/types/protocol";

export const STAGES = [
  ["stt_ms", "Speech to text"],
  ["queue_ms", "Queue"],
  ["nlu_ms", "Understand (NLU)"],
  ["engine_ms", "Engine"],
  ["policy_ms", "Policy"],
  ["nlg_ms", "Phrase (NLG)"],
  ["tts_onset_ms", "Voice onset"],
] as const satisfies readonly (readonly [keyof TraceTimings, string])[];

export interface WaterfallRow {
  stage: string;
  start: number;
  ms: number;
}

export function waterfall(t: TraceTimings): WaterfallRow[] {
  const rows: WaterfallRow[] = [];
  let at = 0;
  for (const [key, label] of STAGES) {
    const v = t[key];
    if (v == null) continue;
    rows.push({ stage: label, start: at, ms: v });
    at += v;
  }
  return rows;
}
