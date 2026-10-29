# Turn-taking and interruption tuning (Phase 65, 2026-10-29)

Harness runs, all `uv run python -m eval.voice --target pipecat --audio inject` (both calls,
real turns, free Groq NLU chain, template/bank NLG, Flux + Aura-2 over WebRTC):

- `before/`: this branch with the two live-test bugs fixed, before any tuning.
- `after-1/`: first tuned run. It showed that a filler at 1.5 s hurts and that the
  leading-silence trim barely fired; both were changed.
- `after/`: final settings (below).
- `long-call/`: one ~10 minute call on the product UI (`--target pipecat-ui`) with a
  server stall in the middle (see "Long call").

Times are ms from the end of the rep's clip unless noted.

| metric | target | before | after-1 | **after** |
|---|---|---|---|---|
| reply turns that got a reply | all | 10 / 13 ¹ | 12 / 13 | **13 / 13** |
| first reply audio p50 / p95 | ≤ 2000 (LLM) / ≤ 3000 | 3385 / 5017 | 3922 / 4724 ² | **3185 / 4829** |
| fast-path turns (`Correct.`) | ≤ 1200 | 985 | 896, 1414 | **1125, 1255** |
| LLM turns: final → reply p50 | – | 2081 | – | **1946** |
| reply → first TTS byte p50 | – | 361 | – | 373 |
| end of turn p50 (eager) | – | 554 (406) | 646 (425) | 651 (**313**) |
| numbers heard | all | 10 / 12 ¹ | 12 / 12 | **12 / 12** |
| first word lost | 0 | 4 ¹ | 1 | **0** |
| split turns / phantom rep lines | 0 | 1 / 1 | 0 / 0 | **0 / 0** |
| false barges | 0 | 0 | 2 ² | **0** |
| backchannels barged (paused) | 0 | 0 / 3 | 0 / 3 (0 / 3) | **0 / 3 (1 / 3)** |
| real interruptions stopped | all | 2 / 2 | 2 / 2 | **2 / 2** |
| barge stop p50 / max, from clip start | – | 1478 / 1753 | 775 / 915 | **689 / 762** |
| barge stop from speech onset, a05 / b05 ³ | ≤ 300 | 1426 / 1049 | 588 / 480 | **435 / 462** |

¹ Call 1 was handed off at its 4th line by the policy (the rep's "all the same amount" was
not read as a payment structure, so the structure question came a third time); its last
three lines got no turn. Not a voice issue; listed as deferred.
² The 1.5 s filler: the reply queued behind it, and after it the harness (like a person)
heard silence and spoke over the late reply.
³ The interrupt clips start with 327 ms (a05) and 154 ms (b05) of silence.

## What changed

- **Barge-in, two steps (user decision, D14).** Silero VAD on the server hears the rep
  start over the agent and pauses the agent's audio (`OutputGate`); Flux's words then
  decide: a real barge (2 non-backchannel words) stops it and drops the rest, a
  backchannel or 0.6 s of quiet resumes where it paused. VAD start 0.1 s; agent audio is
  released only 80 ms ahead of playback so a pause bites at once. Measured: VAD pause
  about 280 to 350 ms after speech onset, agent audio silent 156 to 207 ms after the pause.
  `MIN_BARGE_WORDS` stays 2; backchannels never barged (0 / 3); one paused and resumed.
- **Eager end of turn at 0.4** (was 0.5): fires about 340 ms before the EOT (was about
  150 ms). When the committed transcript adds words while the early turn is still in NLU,
  the words are merged into it (`Orchestrator.merge_into_nlu`) instead of finishing the
  stale turn and redoing it.
- **Flux `eot_threshold` / `eot_timeout_ms` unchanged** (Deepgram defaults 0.7 / 5000):
  no mid-sentence cut-offs in either run (`a04_pause`, `b06_pause` were one turn each),
  and the end of turn is already about 650 ms. Lowering the threshold would trade
  cut-offs for at most a few hundred ms.
- **Leading silence of the reply's first sentence trimmed.** Aura-2 starts each reply
  with 0.1 to 0.3 s of silence (Pipecat's TTFA log). The gate drops it, using Pipecat's
  own onset detector. In `after/` the trim did not fire (Deepgram sends word-timing
  frames between the first audio chunks, which flushed the buffer); fixed after the run,
  confirmed in `long-call/`.
- **Filler only when the reply is late (measured).** At 1.5 s after the final it made
  replies slower (`after-1`: p50 3922). Now at 3.0 s: LLM replies here arrive 1.2 to
  2.9 s after the final, so only outliers get "One moment." (1 in `after/`).
- **Live-test bugs (before tuning):** spoken numbers ("one twenty dollars", "a hundred
  and twenty dollars", "seventy five percent") parse; unconfirmed numbers are read back
  instead of re-asked (user decision); the last line of a handoff or close is played
  before voice ends.

## The gap to the targets

- **Reply latency (p50 3.2 s vs 2.0 s).** After the end of turn (about 0.65 s), the LLM
  turn takes about 1.95 s from final to reply (NLU on the free Groq chain plus NLG), then
  about 0.37 s to Aura-2's first byte and 0.2 to 0.3 s to the browser. Voice adds about
  1.2 s; the rest is NLU. Eager EOT hides about 0.3 s of it. The demo's Claude Haiku NLU
  was not measured (paid calls only to Deepgram in this phase).
- **Barge stop (about 0.45 s vs 0.3 s from onset).** VAD needs about 100 ms of speech plus
  Silero windows and input batching (about 350 ms to the pause), then the 80 ms lead and
  the browser's playout buffer. A shorter VAD start or lead would pause on more noise and
  echo.
- **Fast-path turns (1.1 to 1.3 s vs 1.2 s).** End of turn 0.5 to 0.7 s plus TTS; on target
  within noise.

## Long call

`uv run python -m eval.voice --target pipecat-ui --audio inject --calls numbers_haggle
--gap-s 50 --blip-before b06_pause --blip-s 8 --out .../long-call`: the product UI's
WebRTC client, one call of 8.3 minutes (50 s of silence before each reply line; the call
ended at its last line), and the server process frozen (SIGSTOP) for 8 s before
`b06_pause`, which the browser sees as a network blip.

- **No drift.** First reply audio early in the call 2986 to 4736 ms, late 3250 to 3517 ms;
  end of turn 0.4 to 1.1 s throughout; server memory 167 MB at the start, 128 MB at the end.
- **Blip.** ICE went `disconnected` after 7.8 s of the stall and back to `connected` 0.2 s
  after the server resumed: no new session, no reconnect needed, the call went on. The
  client's full reconnect (new peer connection after `failed`) stays unit-tested.
- **Trim works** (fixed after `after/`): 120 to 300 ms cut from every reply.
- **Barge.** Interruption stopped 566 ms from clip start (412 ms from speech onset);
  backchannels barged 0 / 2, one paused and resumed.
- **One cut-off:** the first line after the stall ("Sixty five percent is … the lowest
  we can go.") was split at its mid-sentence pause into two turns. The same line was one
  turn in `before/` and `after/`. Listed as deferred.
- Mic change (device unplugged / default changed) is unit-tested only; injected audio
  cannot unplug a device.

## Cost

Deepgram, estimated from session seconds and characters spoken: `before` 186 s + 1,457
chars ($0.068), `after-1` 213 s + 1,911 chars ($0.085), `after` 233 s + 1,603 chars
($0.078), `long-call` about 497 s + 901 chars ($0.091). Total about 1,129 s of Flux and
5,872 characters of Aura-2: **about $0.33** of the $10 cap.
