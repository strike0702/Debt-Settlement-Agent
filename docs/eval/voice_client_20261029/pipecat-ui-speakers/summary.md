# Voice harness: pipecat-ui (speakers audio)

Run 2026-10-29 15:05. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 1 |
| first_audio_p50_ms | 7450 |
| first_audio_p95_ms | 7450 |
| any_audio_p50_ms | 7450 |
| eot_p50_ms | 4339 |
| eager_eot_p50_ms | 4290 |
| final_p50_ms | 4339 |
| wer_mean | 1.00 |
| number_recall | 0.00 |
| numbers | 0/12 |
| first_word_lost | 15 |
| lines_scored | 15 |
| split_turns | 0 |
| phantom_finals | 1 |
| false_barges | 0 |
| backchannels_barged | 0/3 |
| interrupts_over_agent | 0/2 |
| interrupts_barged | 0/0 |
| barge_stop_p50_ms | – |
| barge_stop_max_ms | – |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 4339 | 4339 | 6577 | 6923 | 7450 | 7450 | – | – | 0/2 | 1.00 | box. |
| numbers_easy#1 | a03_correct | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a04_pause | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | False | – | 0/1 | 1.00 | – |
| numbers_easy#1 | a06_date | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_easy#1 | a07_yes | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a08_bye | reply | – | – | 26598 | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | – | – | – | – | – | – | – | – | 0/2 | 1.00 | – |
| numbers_haggle#1 | b03_correct | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b04_seventy | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | False | – | 0/1 | 1.00 | – |
| numbers_haggle#1 | b06_pause | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | – | – | – | – | – | – | – | – | 0/2 | 1.00 | – |
| numbers_haggle#1 | b09_yes | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b10_bye | reply | – | – | 26595 | – | – | – | – | – | 0/0 | 1.00 | – |

## Phantom rep lines

- numbers_easy#1: 'box.'

## Driver notes

- numbers_easy#1: a04_pause: agent did not finish a turn before this line
- numbers_easy#1: a05_interrupt: agent never spoke 1200 ms in a row; played anyway
- numbers_easy#1: a06_date: agent did not finish a turn before this line
- numbers_easy#1: a07_yes: agent did not finish a turn before this line
- numbers_easy#1: a08_bye: agent did not finish a turn before this line
- numbers_haggle#1: b03_correct: agent did not finish a turn before this line
- numbers_haggle#1: b04_seventy: agent did not finish a turn before this line
- numbers_haggle#1: b05_interrupt: agent never spoke 1200 ms in a row; played anyway
- numbers_haggle#1: b06_pause: agent did not finish a turn before this line
- numbers_haggle#1: b07_right: agent never spoke 1500 ms in a row; played anyway
- numbers_haggle#1: b08_date: agent did not finish a turn before this line
- numbers_haggle#1: b09_yes: agent did not finish a turn before this line
- numbers_haggle#1: b10_bye: agent did not finish a turn before this line
