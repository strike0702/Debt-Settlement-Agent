# Voice harness: legacy-server (inject audio)

Run 2026-10-28 22:44. Times are ms from the end of the rep's clip unless noted.

| metric | value |
|---|---|
| reply_turns | 26 |
| replied | 21 |
| first_audio_p50_ms | 4463 |
| first_audio_p95_ms | 5172 |
| any_audio_p50_ms | 2913 |
| eot_p50_ms | 756 |
| eager_eot_p50_ms | – |
| final_p50_ms | 1466 |
| wer_mean | 0.09 |
| number_recall | 0.83 |
| numbers | 20/24 |
| first_word_lost | 4 |
| lines_scored | 30 |
| split_turns | 4 |
| phantom_finals | 0 |
| false_barges | 2 |
| backchannels_barged | 2/6 |
| interrupts_over_agent | 0/4 |
| interrupts_barged | 0/0 |
| barge_stop_p50_ms | – |
| barge_stop_max_ms | – |

## Turns

| call | line | kind | eot | final | reply | tts byte | first audio | any audio | barged | stop | numbers | WER | heard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers_easy#1 | a01_mmhm | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_easy#1 | a02_terms | reply | 932 | 1825 | 4584 | – | 5089 | 3066 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least $120 each, al |
| numbers_easy#1 | a03_correct | reply | 666 | 1246 | 1262 | – | 1744 | 1744 | – | – | 0/0 | 0.00 | Correct. |
| numbers_easy#1 | a04_pause | reply | -2210 | -1581 | 256 | – | 1240 | 1240 | – | – | 1/1 | 0.00 | We are looking for  45% of the balance. |
| numbers_easy#1 | a05_interrupt | interrupt | – | – | – | – | – | – | False | – | 1/1 | 0.00 | Hold on, hold on, that is too low. I could do 42 percent. |
| numbers_easy#1 | a06_date | reply | 737 | 1309 | 3569 | – | 5129 | 2988 | – | – | 1/1 | 0.00 | The first payment would be on November 15th |
| numbers_easy#1 | a07_yes | reply | 842 | 1476 | 4350 | – | 4783 | 2722 | – | – | 0/0 | 0.00 | yes that works for us. |
| numbers_easy#1 | a08_bye | reply | 921 | 1558 | 3227 | – | 4501 | 3146 | – | – | 0/0 | 0.00 | No, that's everything. Thanks. |
| numbers_haggle#1 | b01_okay | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b02_terms | reply | 740 | 1483 | 4264 | – | 4768 | 2867 | – | – | 2/2 | 0.00 | we can take up to eight monthly payments, at least $100 each, all the  |
| numbers_haggle#1 | b03_correct | reply | 721 | 1338 | 1343 | – | 1830 | 1830 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#1 | b04_seventy | reply | 976 | 1576 | 3593 | – | – | 3050 | – | – | 1/1 | 0.00 | We're looking for 70% of the balance. |
| numbers_haggle#1 | b05_interrupt | interrupt | – | – | – | – | – | – | False | – | 0/1 | 1.00 | – |
| numbers_haggle#1 | b06_pause | reply | -1228 | -338 | – | – | – | 970 | – | – | 0/1 | 0.25 | 60 o'clock percent is  the lowest we can go. |
| numbers_haggle#1 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 1.00 | – |
| numbers_haggle#1 | b08_date | reply | 739 | 1493 | 3871 | – | 4378 | 2814 | – | – | 2/2 | 0.00 | Can the first payment be on December 1, 2026? |
| numbers_haggle#1 | b09_yes | reply | 973 | 1679 | 3198 | – | 4463 | 3076 | – | – | 0/0 | 0.00 | yes, that works for us. |
| numbers_haggle#1 | b10_bye | reply | 906 | 1691 | 3322 | – | 5050 | 3056 | – | – | 0/0 | 0.00 | No, that's everything. Thanks. |
| numbers_easy#2 | a01_mmhm | backchannel | – | – | – | – | – | – | True | – | 0/0 | 1.00 | – |
| numbers_easy#2 | a02_terms | reply | 978 | 2065 | 4913 | – | 5518 | 3211 | – | – | 2/2 | 0.00 | Sure, we can take up to eight monthly payments, at least $120 each, al |
| numbers_easy#2 | a03_correct | reply | 678 | 1426 | 1432 | – | 1845 | 1845 | – | – | 0/0 | 0.00 | Correct |
| numbers_easy#2 | a04_pause | reply | -2162 | -1482 | – | – | – | 2688 | – | – | 1/1 | 0.00 | we are looking for  45% of the balance. |
| numbers_easy#2 | a05_interrupt | interrupt | – | – | – | – | – | – | False | – | 0/1 | 1.00 | – |
| numbers_easy#2 | a06_date | reply | 804 | 1441 | 3721 | – | 5172 | 2974 | – | – | 1/1 | 0.00 | The first payment would be on November 15th |
| numbers_easy#2 | a07_yes | reply | 859 | 1667 | 3556 | – | 4318 | 2956 | – | – | 0/0 | 0.00 | yes that works for us. |
| numbers_easy#2 | a08_bye | reply | 910 | 1605 | 3820 | – | 4220 | 2994 | – | – | 0/0 | 0.00 | No, that's everything. Thanks. |
| numbers_haggle#2 | b01_okay | backchannel | – | – | – | – | – | – | True | – | 0/0 | 1.00 | – |
| numbers_haggle#2 | b02_terms | reply | 692 | 1364 | 3568 | – | 4366 | 2949 | – | – | 2/2 | 0.00 | we can take up to eight monthly payments, at least $100 each, all the  |
| numbers_haggle#2 | b03_correct | reply | 711 | 1356 | 1368 | – | 1775 | 1775 | – | – | 0/0 | 0.00 | Correct. |
| numbers_haggle#2 | b04_seventy | reply | 895 | 1456 | 3170 | – | – | 2962 | – | – | 1/1 | 0.00 | We're looking for 70% of the balance. |
| numbers_haggle#2 | b05_interrupt | interrupt | – | – | – | – | – | – | True | 310 | 1/1 | 0.09 | Wait, we can't move on that. Still 70%. |
| numbers_haggle#2 | b06_pause | reply | -1400 | -729 | – | – | – | 739 | – | – | 0/1 | 0.25 | 60 o'clock percent is  the lowest we can go. |
| numbers_haggle#2 | b07_right | backchannel | – | – | – | – | – | – | False | – | 0/0 | 0.00 | Right. |
| numbers_haggle#2 | b08_date | reply | 742 | 1375 | 3151 | – | 4572 | 2644 | – | – | 2/2 | 0.00 | Can the first payment be on December 1, 2026? |
| numbers_haggle#2 | b09_yes | reply | 770 | 1478 | 3481 | – | 5010 | 2878 | – | – | 0/0 | 0.00 | yes, that works for us. |
| numbers_haggle#2 | b10_bye | reply | 975 | 1556 | 3080 | – | 4444 | 3044 | – | – | 0/0 | 0.00 | No, that's everything. Thanks. |

## False barge-ins: 2

## Driver notes

- numbers_haggle#1: b06_pause: agent did not finish a turn before this line
- numbers_easy#2: a06_date: agent did not finish a turn before this line
