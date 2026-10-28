# Voice spike: go/no-go (Phase 62, 2026-10-28)

**Verdict: go** for Pipecat + Deepgram (Flux + Aura-2) over WebRTC, with the plan changes below.

## Numbers

Same scripted rep audio (Aura-2 WAVs in `fixtures/voice/`, injected as the mic) and the same
harness (`uv run python -m eval.voice`). Times are in ms from the end of the rep's clip.

| metric | main (server STT, vad-web, browser TTS) | spike (Flux + Aura-2, WebRTC) |
|---|---|---|
| reply turns that got a reply | 21 / 26 | 11 / 13 |
| first reply audio p50 / p95 | 4463 / 5172 | **1940 / 2130** (fixed reply, no NLU) |
| first audio of any kind p50 | 2913 (the "One moment." filler) | 1940 |
| end of turn detected p50 | 756 (silence VAD) | 974 (Flux EndOfTurn) |
| final transcript p50 | 1466 | 974 |
| numbers heard | 20 / 24 | **12 / 12** |
| first word lost | 4 / 30 lines | 2 / 15 (both: "Correct." glued on, see risks) |
| turns split by a mid-sentence pause | 4 | 0 (2 merges from "Correct.", see risks) |
| false barge-ins | 2 (backchannels 2 / 6 barged) | 2 (both from "Correct."); backchannels 0 / 3 |
| real interruptions that stopped the agent | 0 / 4 landed over speech | **2 / 2** |
| barge stop (rep speech start → agent audio stops) | – | 1344 p50 (Flux start of turn ≈ 1.3 s, then ≈ 110 ms to silence) |

Main's own audit log already shows about 3.5 s from VAD end to first audio, trimmed sentence
onsets, and phantom rep lines from the agent's echo (`docs/VOICE_PLAN.md` Context). This harness
confirms the latency (4.5 s to the reply, 2.9 s to the filler), a dropped first word ("No," in
"No, that's everything") and pause splits. Main's voice is being replaced, so the baseline stops
at this one injected-audio run: 2 calls × 2 (`docs/eval/voice_baseline_20261028/legacy-server-inject/`).
No browser-STT or laptop-speaker baseline was run (user decision, to save time).

## Echo on laptop speakers

Real MacBook Air mic and speakers, Chrome `echoCancellation: true` (confirmed in
`track.getSettings()`), with the agent's Aura-2 audio played through the WebRTC `<audio>` element
(`spike-speakers/`). Across about 66 s of agent speech, **1 echo-driven turn start**: a garbled
"She is survived by confusion…", which barged once. Nothing else the agent said became a
transcript. Main's voice (`speechSynthesis`, which echo cancellation does not cover) produced the
"payment, payment…" phantom lines in the user's test.
Caveat: the rep lines were played by `afplay` through the same speakers, and Flux mostly did not
recognize them (WER 0.92). So recognition on speakers is unmeasured here; the echo count stands.

## Aura-2 number reading

12/12 agent lines in today's `Fact.render()` formats were read correctly (`aura_numbers.md`):
`$1,250.50` → "one thousand two hundred and fifty dollars and fifty cents", `45.5%` → "forty five
point five percent", `June 1, 2027` → "June first twenty twenty seven". **No spoken form is
needed in `Fact.render()`**: drop item 7 of Phase 63.

## Risks

1. **One-word turns.** Flux at default settings never ended a turn on a lone "Correct." (2 of 2):
   the word stuck to the rep's next utterance, which caused the splits, the phantom line and both
   false barges. Backchannels ("Okay.", "Right.") did not barge, but they were glued onto the next
   turn. Our agent asks yes/no read-backs, so this matters. Phase 63 must test `eot_timeout_ms`,
   `eot_threshold` and the turn-start handling for one-word answers before anything else.
2. **Barge stop is about 1.3 s**, set by Flux's start of turn (it needs a word or two), against a
   300 ms target. Options: accept it (it also filters backchannels), or add local Silero VAD for
   the stop only while the agent speaks, gated by the echo result above.
3. **Dependency size and Render.** Pipecat 1.14 with `[deepgram,webrtc]` adds about 430 MB to the
   venv (the app's own is 101 MB): numba/llvmlite 143 MB and onnxruntime 76 MB are core
   dependencies; OpenCV 123 MB comes with the `webrtc` extra. Server peak RSS during a call is
   118 MB. Python 3.12 is fine (Pipecat needs ≥ 3.11). Options: (a) take it on Render's free plan
   and check the build time and RAM in Phase 66; (b) use the `webrtc` extra without OpenCV
   (pin our own `aiortc` and import only the transport) if the image is too big.
4. **Render/WebRTC** is untested: free TURN (Phase 66) is still needed. Locally, host candidates
   connected in under 50 ms.

## Plan changes

- Phase 63: drop the `Fact.render()` spoken-form item (Aura-2 reads every format). Add a first
  task: one-word turns and backchannels with Flux (`eot_timeout_ms`, `eot_threshold`), measured
  with this harness (`b03_correct`, `a03_correct`).
- Phase 65: the barge-stop target (≤ 300 ms) needs a decision. Flux alone gives about 1.3 s.
- Phase 66: check the image size and RAM first, and keep option (b) ready.
- Pipecat 1.14 API notes: `DeepgramFluxSTTService(settings=DeepgramFluxSTTService.Settings(...))`
  (`InputParams` is deprecated); `DeepgramTTSService(settings=DeepgramTTSService.Settings(voice=...))`;
  Flux owns turns through `UserTurnProcessor(user_turn_strategies=ExternalUserTurnStrategies())`,
  which broadcasts an interruption at every Flux start of turn, even when the bot is silent
  (count a barge only while the bot speaks). Eager EOT is logged with `eager_eot_threshold` and
  is only acted on with `enable_eager_end_of_turn=True`.

## Cost

Deepgram, from `deepgram_usage.jsonl`: Aura-2 5,432 characters ($0.163), Flux 13.6 min ($0.105),
Nova-3 2.2 min ($0.009). **Total $0.28** of the $5 cap.
