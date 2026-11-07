# Voice harness: pipecat (inject audio)

Run 2026-11-07 15:18. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 13 |
| first_audio_p50_ms | 2596 |
| first_audio_p95_ms | 3569 |
| any_audio_p50_ms | 2596 |
| first_sound_p50_ms | 745 |
| first_sound_p95_ms | 2570 |
| eot_p50_ms | 440 |
| eager_eot_p50_ms | 256 |
| final_p50_ms | 441 |
| wer_mean | 0.01 |
| number_recall | 1.00 |
| numbers | 12/12 |
| first_word_lost | 0 |
| lines_scored | 15 |
| split_turns | 1 |
| phantom_finals | 0 |
| false_barges | 2 |
| backchannels_barged | 0/3 |
| backchannels_paused | 0/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 840 |
| barge_stop_max_ms | 975 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first sound | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 1003 | 1004 | 2565 | 1160 | 1262 | 3293 | 3293 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 416 | 416 | 420 | 430 | 539 | 539 | 539 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 440 | 441 | 1537 | 601 | 702 | 2041 | 2041 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | – | True | 975 | 1/1 | 0.00 | Hold on. Hold on. That is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 886 | 886 | 1940 | 2283 | 2680 | 2680 | 2680 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 434 | 434 | 1871 | 592 | 687 | 2596 | 2596 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_easy#1 | a08_bye | reply | 568 | 569 | 3514 | 725 | 823 | 3983 | 3983 | – | – | 0/0 | 0.20 | No. It's everything. Thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1068 | 1068 | 2246 | 1226 | 1314 | 2926 | 2926 | – | – | 2/2 | 0.00 | We can take up to eight monthly payments, at least one hundred dollars |
| numbers_haggle#1 | b03_correct | reply | 295 | 295 | 296 | 305 | 395 | 395 | 395 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 871 | 871 | 2201 | 1028 | 1128 | 2938 | 2938 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | – | True | 704 | 1/1 | 0.00 | Wait. Wait. We can't move on that. still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | -644 | -643 | 196 | -488 | 2497 | 2497 | 2497 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 372 | 372 | 2756 | 529 | 620 | 3249 | 3249 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six |
| numbers_haggle#1 | b09_yes | reply | 388 | 389 | 1812 | 546 | 635 | 2516 | 2516 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 491 | 491 | 1856 | 648 | 745 | 1955 | 1955 | – | – | 0/0 | 0.00 | No. That's everything. Thanks. |

## False barge-ins: 2
