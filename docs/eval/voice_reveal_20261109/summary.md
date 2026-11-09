# Agent words appear as they are spoken (Phase 74, 2026-11-09)

Aura-2 gives no word timestamps, so the browser reveals each sentence at an estimated pace
(DECISIONS D25). This folder holds how the pace was measured and how close it came on a real call.

## The pace table

`measure_pace.py` spoke 30 of the demo's own sentences with the agent voice (`aura-2-thalia-en`,
1,447 characters, about $0.04, ledgered) and `pace.json` holds each clip's length and silences.
`fit_pace.py` fits the table by least squares on the clip length without its leading silence.

| Token part | ms |
| --- | --- |
| Each letter of a plain word | 58 |
| Each digit of a dollar amount, before the cents | 520 |
| A dollar amount's cents | 1200 |
| One percentage | 577 |
| Each digit of any other number | 265 |
| Pause after a comma, colon or semicolon | 530 |
| Once per sentence, before its first word | 205 |

Leave-one-out error on the clip length: median -31 ms, median absolute 357 ms, p90 absolute 771 ms.

## One call

`call/`: scripted call `numbers_easy` through the product UI on injected audio, Claude Haiku
reading the rep (daily budget set to $0.50). `lag.mjs` reads the page's receive times of
`agent_sentence_start` and `agent_sentence_end` and the app's own table.

**Lag between the text's last word and the end event, 14 sentences played whole: median +128 ms
(the text finished first), median absolute 216 ms, largest 529 ms.** The aim was about 400 ms.

Against the page's audio probe, read by hand from `call/raw.jsonl`:

- The first sentence after a quiet moment starts about 70 ms before the page hears it.
- A later sentence of the same reply starts about 390 ms before the page hears it, because Aura-2's
  leading silence is trimmed only on the first. A word shows when its estimated audio ends, so
  the first word of such a sentence comes at about the right time or a little early.

The barge in that call: the agent went quiet about 2.7 s into "Could you consider 31% of the balance,
amounting to $387.50?", and the bubble kept "Could you consider 31% of the balance" with a cut-off
mark. The ack ("Mm-hm.") sent no events and never showed.

## Reading `call/summary.md`

Its transcription scores (`wer_mean` 1.0, `numbers` 0/5, `first_word_lost` 7) are **not results**.
The harness took the call id from `/voice/offer?call_id=…&warm=1`, so it asked for the wrong call's
server timeline and got nothing (0 server rows in `raw.jsonl`). The call itself went normally: the
web transcript shows the rep's numbers read correctly, one word misheard ("it's" for "that's"), and
the call ended in a deal.
