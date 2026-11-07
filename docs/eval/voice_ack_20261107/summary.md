# Instant ack and sentence audio cache (Phase 70, 2026-11-07)

`uv run python -m eval.voice --target pipecat --audio inject --claude-nlu --tts-cache DIR`: the same
two scripted calls as `voice_haiku_20261107` (Claude Haiku 5.5 reads the rep), now with the
instant ack ("Mm-hm." / "Right.") and the number-free sentence audio cache. `cold/` started with an
empty cache directory; `warm/` reused the directory `cold/` filled (15 entries before, 22 after).

| metric | Haiku baseline (`voice_haiku_20261107`) | cold | **warm** |
|---|---|---|---|
| reply turns that got a reply | 13 / 13 | 13 / 13 | **13 / 13** |
| **first sound** (ack or reply) p50 / p95 | 2484 / 3348 ms ¹ | 883 / 1463 ms | **745 / 2570 ms** ² |
| first reply audio p50 / p95 | 2484 / 3348 ms | 2096 / 3067 ms | 2596 / 3569 ms ³ |
| acks played | – | 15 | 10 |
| TTS first byte after the request: live / cache hit | 313–346 ms / – | 247–330 ms / 2–9 ms | 326–345 ms / 4–11 ms |
| reply sentences served from the cache | – | 3 of 20 | 4 of 20 |
| numbers heard | 12 / 12 | 12 / 12 | 12 / 12 |
| first word lost | 1 | 0 | 0 |
| backchannels barged | 0 / 3 | 0 / 3 | 0 / 3 |
| real interruptions stopped | 2 / 2 | 2 / 2 | 2 / 2 |
| barge stop p50, from clip start | 954 ms | 1235 ms ⁴ | 840 ms |
| false barges (harness) | 0 | 1 | 2 ⁵ |

**Note (follow-up, same day):** these first-sound numbers predate the 0.7 s ack delay
(`voice_ack_delay_ms`, was 0.15 s); expect about +0.55 s on acked turns. Not re-measured.

What the rep hears first on a model turn is now about 0.75–0.9 s after they stop (end of turn
about 0.45 s, ack decision 0.15 s, cached ack audio at once, about 0.1 s to the page), against
about 2.5 s before. The reply itself is not faster: these two calls are about amounts, so most
reply sentences carry a number and always go to live TTS; NLU (about 1.1–1.5 s) still dominates.

¹ The baseline had no ack: its first sound is its first reply audio.

² The p95 is the turns with no ack by design: `a06_date` and `b06_pause` started while the agent
was talking (the harness spoke over a reply, see ⁵), and a turn that interrupts the agent gets no
ack (the rep is usually still talking). Fast-path turns (`Correct.`) get no ack either; their reply
is first (395–539 ms).

³ Not the cache: on the turns that differ, Haiku's NLU took longer (`a08_bye` reply 3514 ms after
the clip, 1921 ms cold) and live Aura-2 first byte plus its leading silence varied (`a07_yes`
725 ms from reply to audio, 401 ms cold). Every reply sentence served from the cache started its
audio within 11 ms. The cold run predates one rule (no ack in a turn that interrupted the agent,
added after it: in `cold/` an ack after `a05`'s barge stretched the measured stop to 1583 ms).

⁴ See ³: the cold run's `a05_interrupt` stop includes an ack played after the barge; that rule
is fixed in the code the warm run measured.

⁵ Harness timing, not the product: `a05_interrupt` and `b05_interrupt` contain a pause, so Flux
ends the turn mid-line (as in the baseline). With a cached question the reply to the first half
plays at once and the rest of the line barges it; the driver then sees 1.2 s of quiet and speaks
the next scripted reply line over the agent's next reply. The driver now ignores ack audio when
deciding that a turn is over (an earlier cold attempt, not kept, desynced on that: 11 / 13
replies).

## How it works

- **Ack**: at Flux's committed end of turn, if no reply is queued 700 ms later
  (`VOICE_ACK_DELAY_MS`; 150 ms when measured) and the rep has not started talking again, the agent says "Mm-hm." or "Right." (rotated). Never before the
  opening, after the call's last line, twice in one turn, while agent audio plays or is paused, or
  in a turn that interrupted the agent. It is pushed straight to TTS: no sentence id, never acked,
  not in the transcript, no rollback effect, and it is not "the agent talking" for barge or pause
  rules. Timeline kind `ack`. Setting `VOICE_ACK` (default on). The 3 s late filler stays.
- **Cache**: exact text of a sentence with no digits, no `$ % € £ ¥ #` and no number words
  (`one`…`million`, ordinals, `half`, `quarter`, `dozen`, `couple`, `percent`, …); key = TTS model
  + voice + sample rate + encoding + text. Raw PCM files in `tts_cache/` next to `DB_PATH`, 50 MB
  (`VOICE_TTS_CACHE_MB`, 0 = off), least recently used evicted. A hit goes through Pipecat's
  same audio context as live audio (started, 40 ms frames, stopped), so heard tracking, the pause
  gate and barge drops are unchanged. A miss fills the cache unless it was interrupted. Audit
  `tts_cache_hit` {sentence_id} for hits, `tts_request` for live sentences; the voice budget counts
  live characters only.

## Listen check (user)

```
uv run uvicorn app.main:app --port 8170
```

Open `http://127.0.0.1:8170`, Start call, Turn voice on, and answer a question with a full
sentence (for example "We can take up to eight monthly payments"). Listen for the short
"Mm-hm." / "Right." about half a second after you stop, then the reply. The second call
replays discovery questions from the cache (`tts_cache/` beside the database). Set
`VOICE_ACK=false` to compare without the ack.

## Raw output

`cold/` and `warm/` hold each run's `summary.md`, `summary.json`, `turns.json`, `timeline.jsonl`,
`raw.jsonl` and `server.log`.
