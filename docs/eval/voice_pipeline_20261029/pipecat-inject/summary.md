# Voice harness: pipecat (inject audio)

Run 2026-10-29 14:32. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 13 |
| first_audio_p50_ms | 3508 |
| first_audio_p95_ms | 5173 |
| any_audio_p50_ms | 3508 |
| eot_p50_ms | 847 |
| eager_eot_p50_ms | 442 |
| final_p50_ms | 847 |
| wer_mean | 0.01 |
| number_recall | 1.00 |
| numbers | 12/12 |
| first_word_lost | 0 |
| lines_scored | 15 |
| split_turns | 0 |
| phantom_finals | 0 |
| false_barges | 0 |
| backchannels_barged | 0/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 1390 |
| barge_stop_max_ms | 1594 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 1097 | 1097 | 2695 | 3036 | 3508 | 3508 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 961 | 962 | 964 | 1312 | 1609 | 1609 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 449 | 449 | 3226 | 3565 | 3833 | 3833 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | True | 1594 | 1/1 | 0.00 | Hold on. Hold on. That is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 1026 | 1026 | 2653 | 2994 | 3205 | 3205 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 625 | 626 | 2245 | 2590 | 2982 | 2982 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_easy#1 | a08_bye | reply | 632 | 633 | 4098 | 4434 | 4640 | 4640 | – | – | 0/0 | 0.20 | No. It's everything. Thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1076 | 1077 | 2822 | 3141 | 3529 | 3529 | – | – | 2/2 | 0.00 | We can take up to eight monthly payments, at least one hundred dollars |
| numbers_haggle#1 | b03_correct | reply | 418 | 418 | 420 | 757 | 1080 | 1080 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 847 | 847 | 2771 | 3100 | 3560 | 3560 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 1187 | 1/1 | 0.00 | Wait. Wait. We can't move on that. still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | 1065 | 1065 | 2830 | 3178 | 3358 | 3358 | – | – | 1/1 | 0.00 | sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 529 | 530 | 4829 | 5152 | 5337 | 5337 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six? |
| numbers_haggle#1 | b09_yes | reply | 1573 | 1573 | 2610 | 2934 | 3145 | 3145 | – | – | 0/0 | 0.00 | Yes, that works for us. |
| numbers_haggle#1 | b10_bye | reply | 498 | 498 | 4525 | 4847 | 5064 | 5064 | – | – | 0/0 | 0.00 | No. That's everything. Thanks. |
