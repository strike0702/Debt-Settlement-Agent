# Voice harness: pipecat (inject audio)

Run 2026-10-29 22:12. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 10 |
| first_audio_p50_ms | 3385 |
| first_audio_p95_ms | 5017 |
| any_audio_p50_ms | 3385 |
| eot_p50_ms | 554 |
| eager_eot_p50_ms | 406 |
| final_p50_ms | 554 |
| wer_mean | 0.28 |
| number_recall | 0.83 |
| numbers | 10/12 |
| first_word_lost | 4 |
| lines_scored | 15 |
| split_turns | 1 |
| phantom_finals | 1 |
| false_barges | 0 |
| backchannels_barged | 0/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 1478 |
| barge_stop_max_ms | 1753 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 1071 | 1071 | 2888 | 3257 | 3437 | 3437 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred t |
| numbers_easy#1 | a03_correct | reply | 490 | 491 | 2367 | 2772 | 2881 | 2881 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | 447 | 448 | 2009 | 2382 | 2797 | 2797 | – | – | 1/1 | 0.00 | We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | True | 1753 | 0/1 | 1.00 | – |
| numbers_easy#1 | a06_date | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_easy#1 | a07_yes | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a08_bye | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1166 | 1166 | 3300 | 3652 | 4170 | 4170 | – | – | 2/2 | 0.00 | We can take up to eight monthly payments, at least one hundred dollars |
| numbers_haggle#1 | b03_correct | reply | 449 | 449 | 451 | 804 | 985 | 985 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 970 | 971 | 1927 | 2295 | 2454 | 2454 | – | – | 1/1 | 0.00 | We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 1203 | 1/1 | 0.00 | Wait. Wait. We can't move on that. still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | 991 | 991 | 3154 | 3522 | 3722 | 3722 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 600 | 601 | 4610 | 4964 | 5386 | 5386 | – | – | 2/2 | 0.00 | Can the first payment be on December first twenty twenty six? |
| numbers_haggle#1 | b09_yes | reply | 508 | 508 | 2590 | 2944 | 3334 | 3334 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 55 | 56 | 3817 | 4170 | 4566 | 4566 | – | – | 0/0 | 0.20 | No. That's everything. effects. |

## Phantom rep lines

- numbers_haggle#1: 'effects.'

## Driver notes

- numbers_easy#1: a06_date: agent did not finish a turn before this line
- numbers_easy#1: a07_yes: agent did not finish a turn before this line
- numbers_easy#1: a08_bye: agent did not finish a turn before this line
