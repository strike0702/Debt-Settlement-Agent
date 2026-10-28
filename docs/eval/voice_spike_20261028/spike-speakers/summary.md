# Voice harness: spike (speakers audio)

Run 2026-10-28 23:06. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 13 |
| replied | 5 |
| first_audio_p50_ms | 2462 |
| first_audio_p95_ms | 6701 |
| any_audio_p50_ms | 2462 |
| eot_p50_ms | 848 |
| eager_eot_p50_ms | 40 |
| final_p50_ms | 850 |
| wer_mean | 0.92 |
| number_recall | 0.08 |
| numbers | 1/12 |
| first_word_lost | 13 |
| lines_scored | 15 |
| split_turns | 0 |
| phantom_finals | 3 |
| false_barges | 1 |
| backchannels_barged | 0/3 |
| interrupts_over_agent | 1/2 |
| interrupts_barged | 0/1 |
| barge_stop_p50_ms | – |
| barge_stop_max_ms | – |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | -1938 | -1936 | -1936 | -1480 | 3139 | 3139 | – | – | 0/2 | 0.94 | Sure. |
| numbers_easy#1 | a03_correct | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a04_pause | reply | 791 | 793 | 793 | 1163 | 1619 | 1619 | – | – | 1/1 | 0.78 | Forty five percent. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | False | – | 0/1 | 1.00 | – |
| numbers_easy#1 | a06_date | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_easy#1 | a07_yes | reply | 1670 | 1672 | 1672 | 2046 | 2462 | 2462 | – | – | 0/0 | 1.00 | Just |
| numbers_easy#1 | a08_bye | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | – | – | – | – | – | – | – | – | 0/2 | 1.00 | – |
| numbers_haggle#1 | b03_correct | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b04_seventy | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | False | – | 0/1 | 1.00 | – |
| numbers_haggle#1 | b06_pause | reply | – | – | – | – | – | – | – | – | 0/1 | 1.00 | – |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 6989 | 6991 | 6991 | 7378 | 7591 | 7591 | – | – | 0/2 | 1.00 | six. |
| numbers_haggle#1 | b09_yes | reply | 848 | 850 | 850 | 1229 | 1642 | 1642 | – | – | 0/0 | 0.00 | Yes. That works for us. |
| numbers_haggle#1 | b10_bye | reply | – | – | – | – | – | – | – | – | 0/0 | 1.00 | – |

## Phantom rep lines

- numbers_easy#1: 'Just'
- numbers_haggle#1: 'She is survived by confusion, bitter planning was observed, tired by seeing her playing'
- numbers_haggle#1: 'six.'

## False barge-ins: 1

## Driver notes

- numbers_easy#1: a04_pause: agent did not finish a turn before this line
- numbers_easy#1: a06_date: agent did not finish a turn before this line
- numbers_easy#1: a07_yes: agent did not finish a turn before this line
- numbers_haggle#1: b01_okay: agent never spoke 3000 ms in a row; played anyway
- numbers_haggle#1: b03_correct: agent did not finish a turn before this line
- numbers_haggle#1: b04_seventy: agent did not finish a turn before this line
- numbers_haggle#1: b05_interrupt: agent never spoke 1200 ms in a row; played anyway
- numbers_haggle#1: b06_pause: agent did not finish a turn before this line
- numbers_haggle#1: b07_right: agent never spoke 1500 ms in a row; played anyway
- numbers_haggle#1: b08_date: agent did not finish a turn before this line
