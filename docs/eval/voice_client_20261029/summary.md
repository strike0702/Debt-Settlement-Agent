# Browser voice client on WebRTC (Phase 64, 2026-10-29)

One run of the Phase 62 harness through the product UI's new WebRTC client, on laptop
speakers: `uv run python -m eval.voice --target pipecat-ui --audio speakers --out
docs/eval/voice_client_20261029/pipecat-ui-speakers`. The harness rebuilds `web/dist`,
starts the app with `VOICE_STACK=pipecat` (free Groq NLU chain, bank NLG), clicks
Start call, then Turn voice on, and plays each rep WAV with `afplay` into the real
laptop mic. Chrome's own echo cancellation is on (`getUserMedia` constraints; the
page logs `track.getSettings()` once).

| metric | value |
|---|---|
| client connected (WebRTC), both calls | yes |
| agent audio heard on the page (`agent_audio_on`) | 6 stretches (both openings and the replies) |
| rep lines recognized by Flux | 1 / 18 (one phantom final, "box.") |
| replies to a rep line | 1 / 13 (first audio 7450 ms) |
| echo turns (agent's words transcribed as the rep) | 0 |
| false barges | 0 |

## What this shows

- The client's own path works end to end: signaling, connect, agent audio out, call
  states, hang-up (the UI review session showed Connecting → Listening → Thinking →
  Agent speaking → Listening, mute and push to talk).
- With speakers, about 7.4 minutes of Flux and no echo turn and no false barge:
  Chrome's echo cancellation kept the agent out of the mic.
- The numbers are not a latency or accuracy result. The rep lines played by `afplay`
  from the same laptop were almost never heard by the mic stream. Phase 62 saw the same
  thing on the spike page ("on speakers, afplay-played rep lines were mostly not
  recognized"). The likeliest cause is Chrome's echo canceller and noise suppression
  treating the laptop's own loudspeaker output as echo: `afplay` plays on the same
  output device the canceller references. A real rep talking into the mic is not
  played through that device. The user's voice test before merge is the real check.
  Phase 63's injected-audio run (13/13 replies, numbers 12/12) still stands for the
  server path.

## Cost

Harness: Flux 443.9 s ($0.057), Aura-2 672 characters ($0.020). UI review session
(screenshots, fake silent mic): Flux 112.4 s ($0.014), Aura-2 492 characters ($0.015).
About **$0.106** this phase; ledger total $0.47 of the $5 cap
(`docs/eval/voice_spike_20261028/deepgram_usage.jsonl`).
