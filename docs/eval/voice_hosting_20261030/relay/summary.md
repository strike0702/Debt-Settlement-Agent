# Voice harness: pipecat-ui (inject audio)

Run 2026-10-30 15:49. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 6 |
| replied | 6 |
| first_audio_p50_ms | 3748 |
| first_audio_p95_ms | 4544 |
| any_audio_p50_ms | 3748 |
| eot_p50_ms | 674 |
| eager_eot_p50_ms | 478 |
| final_p50_ms | 674 |
| wer_mean | 0.03 |
| number_recall | 1.00 |
| numbers | 5/5 |
| first_word_lost | 0 |
| lines_scored | 7 |
| split_turns | 0 |
| phantom_finals | 0 |
| false_barges | 0 |
| backchannels_barged | 0/1 |
| backchannels_paused | 0/1 |
| interrupts_over_agent | 1/1 |
| interrupts_barged | 1/1 |
| barge_stop_p50_ms | 1314 |
| barge_stop_max_ms | 1314 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 1179 | 1179 | 2865 | 3254 | 4080 | 4080 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 568 | 568 | 571 | 936 | 1471 | 1471 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 565 | 565 | 2881 | 3246 | 3938 | 3938 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | True | 1314 | 1/1 | 0.00 | Hold on. Hold on. That is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 1076 | 1077 | 2486 | 2868 | 3558 | 3558 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 630 | 630 | 2097 | 2472 | 2888 | 2888 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_easy#1 | a08_bye | reply | 718 | 718 | 3672 | 4054 | 4699 | 4699 | – | – | 0/0 | 0.20 | No, it's everything. Thanks. |
