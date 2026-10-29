# Voice harness: pipecat (inject audio)

Run 2026-10-29 22:48. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 13 |
| first_audio_p50_ms | 3185 |
| first_audio_p95_ms | 4829 |
| any_audio_p50_ms | 3185 |
| eot_p50_ms | 651 |
| eager_eot_p50_ms | 313 |
| final_p50_ms | 651 |
| wer_mean | 0.01 |
| number_recall | 1.00 |
| numbers | 12/12 |
| first_word_lost | 0 |
| lines_scored | 15 |
| split_turns | 0 |
| phantom_finals | 0 |
| false_barges | 0 |
| backchannels_barged | 0/3 |
| backchannels_paused | 1/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 689 |
| barge_stop_max_ms | 762 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 1048 | 1048 | 3554 | 3928 | 4440 | 4440 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 481 | 481 | 483 | 848 | 1125 | 1125 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 430 | 431 | 2302 | 2672 | 2874 | 2874 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | True | 762 | 1/1 | 0.00 | Hold on. Hold on. That is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 1070 | 1070 | 2241 | 2619 | 2804 | 2804 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 651 | 651 | 2654 | 3026 | 3185 | 3185 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_easy#1 | a08_bye | reply | 647 | 648 | 3577 | 3966 | 4412 | 4412 | – | – | 0/0 | 0.20 | No. It's everything. Thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1026 | 1026 | 2846 | 3226 | 3862 | 3862 | – | – | 2/2 | 0.00 | We can take up to eight monthly payments, at least one hundred dollars |
| numbers_haggle#1 | b03_correct | reply | 672 | 672 | 675 | 1056 | 1255 | 1255 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 1009 | 1009 | 2646 | 3017 | 3139 | 3139 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 616 | 1/1 | 0.00 | Wait. Wait. We can't move on that. still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | 1126 | 1126 | 3072 | 3446 | 3979 | 3979 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 618 | 618 | 4516 | 4013 | 5411 | 4220 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six? |
| numbers_haggle#1 | b09_yes | reply | 557 | 557 | 2826 | 3201 | 3655 | 3655 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 638 | 639 | 2333 | 2706 | 2800 | 2800 | – | – | 0/0 | 0.00 | No. That's everything. Thanks. |
