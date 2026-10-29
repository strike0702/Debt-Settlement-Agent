# Voice harness: pipecat (inject audio)

Run 2026-10-29 22:42. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 12 |
| first_audio_p50_ms | 3922 |
| first_audio_p95_ms | 4724 |
| any_audio_p50_ms | 2654 |
| eot_p50_ms | 646 |
| eager_eot_p50_ms | 425 |
| final_p50_ms | 646 |
| wer_mean | 0.02 |
| number_recall | 1.00 |
| numbers | 12/12 |
| first_word_lost | 1 |
| lines_scored | 15 |
| split_turns | 0 |
| phantom_finals | 0 |
| false_barges | 2 |
| backchannels_barged | 0/3 |
| backchannels_paused | 0/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 775 |
| barge_stop_max_ms | 915 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 1084 | 1084 | 3945 | 2969 | 4544 | 3195 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 331 | 331 | 334 | 707 | 896 | 896 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 434 | 434 | 2013 | 2295 | 3444 | 2691 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | True | 915 | 1/1 | 0.00 | Hold on. Hold on. That is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 1016 | 1016 | 2790 | 2865 | 4128 | 2978 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 542 | 543 | 2616 | 2415 | 3731 | 2611 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_easy#1 | a08_bye | reply | 646 | 646 | 3548 | 2533 | 4112 | 2632 | – | – | 0/0 | 0.20 | No. It's everything. Thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1179 | 1180 | 3043 | 3054 | 4166 | 3246 | – | – | 2/2 | 0.06 | can take up to eight monthly payments, at least one hundred dollars ea |
| numbers_haggle#1 | b03_correct | reply | 433 | 433 | 434 | 778 | 1414 | 1414 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 983 | 983 | 2464 | 2856 | 3072 | 3072 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 634 | 1/1 | 0.00 | Wait. Wait. We can't move on that. Still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | 1168 | 1168 | 2852 | 3030 | 4187 | 3438 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 544 | 545 | 5074 | 2416 | – | 2507 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six? |
| numbers_haggle#1 | b09_yes | reply | 1501 | 1501 | – | – | 236 | 236 | – | – | 0/0 | 0.00 | Yes, that works for us. |
| numbers_haggle#1 | b10_bye | reply | 566 | 566 | -1353 | -980 | 4944 | 2654 | – | – | 0/0 | 0.00 | No. That's everything. Thanks. |

## False barge-ins: 2
