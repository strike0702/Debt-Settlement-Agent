# Voice harness: pipecat-ui (inject audio)

Run 2026-10-29 22:58. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 7 |
| replied | 7 |
| first_audio_p50_ms | 3517 |
| first_audio_p95_ms | 5020 |
| any_audio_p50_ms | 3517 |
| eot_p50_ms | 498 |
| eager_eot_p50_ms | 381 |
| final_p50_ms | 499 |
| wer_mean | 0.00 |
| number_recall | 1.00 |
| numbers | 7/7 |
| first_word_lost | 0 |
| lines_scored | 8 |
| split_turns | 1 |
| phantom_finals | 0 |
| false_barges | 0 |
| backchannels_barged | 0/2 |
| backchannels_paused | 1/2 |
| interrupts_over_agent | 1/1 |
| interrupts_barged | 1/1 |
| barge_stop_p50_ms | 566 |
| barge_stop_max_ms | 566 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1106 | 1107 | 3984 | 4355 | 4736 | 4736 | – | – | 2/2 | 0.00 | We can take up to eight monthly payments, at least one hundred dollars |
| numbers_haggle#1 | b03_correct | reply | 417 | 418 | 421 | 916 | 1316 | 1316 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 990 | 990 | 2231 | 2565 | 2986 | 2986 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 566 | 1/1 | 0.00 | Wait. Wait. We can't move on that. still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | -656 | -655 | 2879 | 3225 | 3990 | 3990 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 442 | 443 | 4599 | 3792 | 5141 | 3968 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six |
| numbers_haggle#1 | b09_yes | reply | 498 | 499 | 2736 | 3081 | 3517 | 3517 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 626 | 626 | 2466 | 2828 | 3250 | 3250 | – | – | 0/0 | 0.00 | No. That's everything. Thanks. |
