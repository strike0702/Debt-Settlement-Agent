# Voice harness: pipecat (inject audio)

Run 2026-11-07 15:14. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 13 |
| first_audio_p50_ms | 2096 |
| first_audio_p95_ms | 3067 |
| any_audio_p50_ms | 2096 |
| first_sound_p50_ms | 883 |
| first_sound_p95_ms | 1463 |
| eot_p50_ms | 420 |
| eager_eot_p50_ms | 390 |
| final_p50_ms | 420 |
| wer_mean | 0.01 |
| number_recall | 1.00 |
| numbers | 12/12 |
| first_word_lost | 0 |
| lines_scored | 15 |
| split_turns | 1 |
| phantom_finals | 0 |
| false_barges | 1 |
| backchannels_barged | 0/3 |
| backchannels_paused | 0/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 1235 |
| barge_stop_max_ms | 1583 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first sound | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 941 | 942 | 2408 | 1344 | 1646 | 2786 | 2786 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 340 | 342 | 349 | 599 | 893 | 893 | 893 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 349 | 350 | 1625 | 753 | 1057 | 2017 | 2017 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | – | True | 1583 | 1/1 | 0.00 | Hold on. Hold on. that is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 910 | 910 | 1706 | 1066 | 1176 | 2096 | 2096 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 420 | 420 | 1606 | 572 | 667 | 2007 | 2007 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_easy#1 | a08_bye | reply | 627 | 627 | 1921 | 782 | 883 | 2485 | 2485 | – | – | 0/0 | 0.20 | No. It's everything. Thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1094 | 1094 | 2273 | 1253 | 1341 | 2379 | 2379 | – | – | 2/2 | 0.00 | We can take up to eight monthly payments, at least one hundred dollars |
| numbers_haggle#1 | b03_correct | reply | 395 | 395 | 397 | 403 | 507 | 507 | 507 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 1080 | 1081 | 1760 | 1239 | 1338 | 2227 | 2227 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | – | True | 886 | 1/1 | 0.00 | Wait. Wait. We can't move on that. Still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | -632 | -632 | -85 | -477 | 365 | 365 | 365 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 243 | 244 | 2838 | 402 | 498 | 3489 | 3489 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six |
| numbers_haggle#1 | b09_yes | reply | 372 | 372 | 1687 | 532 | 624 | 2344 | 2344 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 496 | 497 | 1540 | 650 | 733 | 1663 | 1663 | – | – | 0/0 | 0.00 | No. That's everything. Thanks. |

## False barge-ins: 1
