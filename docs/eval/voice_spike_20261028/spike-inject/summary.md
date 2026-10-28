# Voice harness: spike (inject audio)

Run 2026-10-28 22:59. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 11 |
| first_audio_p50_ms | 1940 |
| first_audio_p95_ms | 2130 |
| any_audio_p50_ms | 1940 |
| eot_p50_ms | 974 |
| eager_eot_p50_ms | 941 |
| final_p50_ms | 974 |
| wer_mean | 0.16 |
| number_recall | 1.00 |
| numbers | 12/12 |
| first_word_lost | 2 |
| lines_scored | 15 |
| split_turns | 2 |
| phantom_finals | 1 |
| false_barges | 2 |
| backchannels_barged | 0/3 |
| interrupts_over_agent | 2/2 |
| interrupts_barged | 2/2 |
| barge_stop_p50_ms | 1344 |
| barge_stop_max_ms | 1378 |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 1517 | 1518 | 1518 | 1867 | 2079 | 2079 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least one hundred a |
| numbers_easy#1 | a03_correct | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a04_pause | reply | 373 | 373 | 373 | 735 | 1006 | 1006 | – | – | 1/1 | 0.11 | Correct. We're looking for forty five percent of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | True | 1378 | 1/1 | 0.00 | Hold on. Hold on. That is too low. I could do forty two percent. |
| numbers_easy#1 | a06_date | reply | 1489 | 1489 | 1489 | 1874 | 1966 | 1966 | – | – | 1/1 | 0.00 | The first payment would be on November fifteenth. |
| numbers_easy#1 | a07_yes | reply | 974 | 974 | 974 | 1320 | 1421 | 1421 | – | – | 0/0 | 0.00 | Yes, that works for us. |
| numbers_easy#1 | a08_bye | reply | 1007 | 1007 | 1007 | 1355 | 1840 | 1840 | – | – | 0/0 | 0.00 | No, that's everything, thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 1484 | 1484 | 1484 | 1842 | 2045 | 2045 | – | – | 2/2 | 0.06 | Okay. We can take up to eight monthly payments, at least one hundred d |
| numbers_haggle#1 | b03_correct | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b04_seventy | reply | -1873 | -1872 | -1872 | -1503 | 1940 | 1940 | – | – | 1/1 | 0.11 | Correct. We're looking for seventy percent of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 1310 | 1/1 | 0.00 | Wait wait, we can't move on that. Still seventy percent. |
| numbers_haggle#1 | b06_pause | reply | -627 | -625 | -625 | -275 | 2182 | 2182 | – | – | 1/1 | 0.00 | Sixty five percent is the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 954 | 955 | 955 | 1336 | 1448 | 1448 | – | – | 2/2 | 0.12 | Right. Can the first payment be on December first twenty twenty six? |
| numbers_haggle#1 | b09_yes | reply | 944 | 946 | 946 | 1306 | 1753 | 1753 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | 1020 | 1021 | 1021 | 1402 | 2008 | 2008 | – | – | 0/0 | 0.00 | No, that's everything, thanks. |

## Phantom rep lines

- numbers_haggle#1: 'Correct.'

## False barge-ins: 2

## Driver notes

- numbers_easy#1: a04_pause: agent did not finish a turn before this line
- numbers_haggle#1: b04_seventy: agent did not finish a turn before this line
