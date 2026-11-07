# Voice latency with Claude Haiku reading the rep (2026-11-07)

`uv run python -m eval.voice --target pipecat --audio inject --claude-nlu`: the same two scripted
calls and settings as Phase 65's final run (`voice_tuned_20261029/after/`), but with the demo's
paid Claude Haiku 5.5 NLU first instead of the free Groq chain. Main at `36ac962`.

| metric | Groq (Phase 65 `after/`) | **Claude Haiku (this run)** |
|---|---|---|
| reply turns that got a reply | 13 / 13 | **13 / 13** |
| first reply audio p50 / p95 | 3185 / 4829 ms | **2484 / 3348 ms** |
| NLU time on model turns, range | 1704–2282 ms | **1140–1855 ms** (p50 about 1.3 s) |
| fast-path turns (`Correct.`) | 1125, 1255 ms | 807, 1142 ms |
| numbers heard | 12 / 12 | 12 / 12 |
| first word lost | 0 | 1 |
| false barges / backchannels barged | 0 / 0 of 3 | 0 / 0 of 3 |
| real interruptions stopped | 2 / 2 | 2 / 2 |
| barge stop p50, from clip start | 689 ms | 954 ms ¹ |

Targets were 2000 / 3000 ms p50 / p95: p95 now nearly meets 3000, p50 is still about 0.5 s over.
What is left per model turn: end of turn about 0.5 s, NLU about 1.3 s, first TTS byte about 0.35 s,
first audio about 0.3 s after that.

¹ The barge pause is less sensitive since the user's noise check (D16: 0.3 s of speech,
confidence 0.75, volume 0.7, was 0.1 s), so a real interruption stops later than in Phase 65.

The NLU provider is not named in the harness logs; the times match Haiku's earlier corpus
measurement (1.17 s p50) and are well under the Groq run's.

## Raw harness output

Run 2026-11-07 14:39. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 13 |
| first_audio_p50_ms | 2484 |
| first_audio_p95_ms | 3348 |
| any_audio_p50_ms | 2484 |
| eot_p50_ms | 529 |
| eager_eot_p50_ms | 428 |
| final_p50_ms | 530 |
| wer_mean | 0.02 |
| number_recall | 1.00 |
| numbers | 12/12 |
| first_word_lost | 1 |
| lines_scored | 15 |
| split_turns | 0 |
| phantom_finals | 0 |
| false_barges | 0 |
| backchannels_barged | 0/3 |
| backchannels_paused | 0/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 954 |
| barge_stop_max_ms | 1047 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 959 | 960 | 2134 | 2465 | 2850 | 2850 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 432 | 433 | 435 | 774 | 1142 | 1142 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 417 | 417 | 1649 | 1988 | 2158 | 2158 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | True | 1047 | 1/1 | 0.00 | Hold on. Hold on. That is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 946 | 946 | 2410 | 2756 | 3386 | 3386 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 494 | 494 | 1906 | 2248 | 2399 | 2399 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_easy#1 | a08_bye | reply | 529 | 530 | 2371 | 2698 | 3115 | 3115 | – | – | 0/0 | 0.20 | No, it's everything. Thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1149 | 1149 | 2082 | 2402 | 3205 | 3205 | – | – | 2/2 | 0.06 | can take up to eight monthly payments, at least one hundred dollars ea |
| numbers_haggle#1 | b03_correct | reply | 338 | 339 | 341 | 656 | 807 | 807 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 867 | 867 | 2001 | 2314 | 2471 | 2471 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 861 | 1/1 | 0.00 | Wait. Wait. We can't move on that. still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | 1248 | 1248 | 2559 | 2880 | 3023 | 3023 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 269 | 269 | 2838 | 3165 | 3324 | 3324 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six |
| numbers_haggle#1 | b09_yes | reply | 438 | 438 | 1674 | 2000 | 2373 | 2373 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 620 | 620 | 1794 | 2116 | 2484 | 2484 | – | – | 0/0 | 0.00 | No, that's everything. Thanks. |
