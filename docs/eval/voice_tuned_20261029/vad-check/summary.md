# Voice harness: pipecat (inject audio)

Run 2026-10-29 23:12. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 7 |
| replied | 7 |
| first_audio_p50_ms | 3550 |
| first_audio_p95_ms | 5818 |
| any_audio_p50_ms | 3550 |
| eot_p50_ms | 690 |
| eager_eot_p50_ms | 496 |
| final_p50_ms | 690 |
| wer_mean | 0.00 |
| number_recall | 1.00 |
| numbers | 7/7 |
| first_word_lost | 0 |
| lines_scored | 8 |
| split_turns | 1 |
| phantom_finals | 0 |
| false_barges | 2 |
| backchannels_barged | 0/2 |
| backchannels_paused | 0/2 |
| interrupts_over_agent | 1/1 |
| interrupts_barged | 1/1 |
| barge_stop_p50_ms | 816 |
| barge_stop_max_ms | 816 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1043 | 1043 | 3023 | 3407 | 3550 | 3550 | – | – | 2/2 | 0.00 | We can take up to eight monthly payments, at least one hundred dollars |
| numbers_haggle#1 | b03_correct | reply | 734 | 735 | 738 | 1128 | 1256 | 1256 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 1033 | 1033 | 2300 | 2682 | 3462 | 3462 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 816 | 1/1 | 0.00 | Wait. Wait. We can't move on that. still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | -539 | -539 | 3782 | 4186 | 4293 | 4293 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 657 | 657 | 5660 | 4065 | 6472 | 4192 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six? |
| numbers_haggle#1 | b09_yes | reply | 541 | 542 | – | -1042 | 20 | 20 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 690 | 690 | -1090 | -698 | 3654 | 3654 | – | – | 0/0 | 0.00 | No, that's everything. Thanks. |

## False barge-ins: 2
