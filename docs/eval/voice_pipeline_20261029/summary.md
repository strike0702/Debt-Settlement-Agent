# Server voice pipeline (Phase 63, 2026-10-29)

One run of the Phase 62 harness against the app with `VOICE_STACK=pipecat`:
`uv run python -m eval.voice --target pipecat --audio inject --out docs/eval/voice_pipeline_20261029/pipecat-inject`.
It is a real call: orchestrator, free Groq NLU chain (`CLAUDE_DAILY_BUDGET_USD=0`),
bank NLG, Flux + Aura-2 over WebRTC, the minimal page `/voice/test`. Times are in ms
from the end of the rep's clip.

| metric | main (Phase 62 baseline) | spike (fixed reply, no NLU) | **pipeline (real turn)** |
|---|---|---|---|
| reply turns that got a reply | 21 / 26 | 11 / 13 | **13 / 13** |
| first reply audio p50 / p95 | 4463 / 5172 | 1940 / 2130 | **3508 / 5173** |
| end of turn p50 | 756 | 974 | 847 (eager EOT 442) |
| numbers heard | 20 / 24 | 12 / 12 | **12 / 12** |
| first word lost | 4 / 30 | 2 / 15 | **0 / 15** |
| split turns | 4 | 0 (2 merges) | **0** |
| false barges | 2 | 2 | **0** |
| backchannels barged | 2 / 6 | 0 / 3 | 0 / 3 |
| real interruptions stopped | 0 / 4 | 2 / 2 | 2 / 2 |
| barge stop p50 / max | – | 1344 | 1390 / 1594 |

## One-word turns (the first task)

"Correct." was its own turn in both calls (`a03_correct` 1.6 s to audio, `b03_correct`
1.1 s). Flux was never the cause: streamed directly, a lone "Correct." gets StartOfTurn
and EndOfTurn (EOT confidence 0.70 to 0.76) at default settings, with or without
`eager_eot_threshold`, and also after a prior turn and 8 s of silence. The spike's
server log shows 20 "No audio received ... Sending silence" warnings: the WebRTC input
delivers no frames while the browser sends silence, and Pipecat's Flux watchdog fills
gaps only after a start of turn. A short word therefore got no trailing audio and waited
for the rep's next line. `SilenceFill` sends zeros through any input gap over 60 ms.
`eot_threshold` and `eot_timeout_ms` stay at Deepgram's defaults (settings exist, unset).

## What is left for Phase 65

- Reply audio is 3.5 s p50. About 1.6 to 4 s of that is NLU + NLG after the EOT (the free
  Groq chain here). Eager EOT fired about 400 ms before the EOT; its reuse was not measured
  per turn.
- Barge stop is still about 1.4 s (Flux start of turn plus `MIN_BARGE_WORDS = 2`).

## Cost

Flux 235 s ($0.030), Aura-2 about 1,700 characters (estimate from 34 sentences, $0.051),
direct Flux probes for the one-word check 44 s ($0.006). About **$0.087** this phase; ledger
total $0.36 of the $5 cap (`docs/eval/voice_spike_20261028/deepgram_usage.jsonl`).
