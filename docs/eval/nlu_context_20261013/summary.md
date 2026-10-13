# NLU turn context on Claude Haiku 5.5 (Phase 54, 2026-10-13)

## Decision

**Dropped from the live prompt** (user decision, 2026-10-13). The turns gave
no measured gain: terms went from 14/16 to 13/16 on the context set, and each
turn cost about 79 more prompt tokens. Code fixes A–C in Phase 54 cover the
live call (change request applied directly, a short pick settles an open
clarify, talked-over questions count). The shipped NLU prompt is again
byte-identical to the one behind `HAIKU_P43_FIX`. The context could be retried
later with different wording. The corpus `context` field,
`tests/nlu_corpus_context.jsonl` and the records stay. `eval.nlu_corpus
--with-context` adds the measured block and sentence back, eval only, so
`CONTEXT_ON_P54` can be reproduced exactly.

## What was measured

The NLU prompt now gets the last 3 spoken turns before the rep's line, as
earlier context, plus one system sentence: "Use earlier turns only to
understand references ("it", "that", a bare value answering a question);
extract only what the Rep utterance says." Quotes must still be verbatim
substrings of the current utterance (`post_verify` unchanged). With no context
the user message is byte-for-byte the old one, so the main corpus measures the
new sentence alone.

This was measured because of a live call (audit `9a1e7d40`). After "Got it,
at least $180 per payment.", the rep said "Sorry, can you change it to $250?".
The agent asked "which is it" twice and then "is $250 the total settlement, or
the minimum for each payment?". The code fixes for that call are in
`docs/history/PROGRESS.md` (Phase 54). This page covers the model side only.

## Runs

Haiku only, through the eval-only providers file (one paid target, no
fallback, effort low). Fresh response-cache keys, because the prompt changed.
0 errors and 0 fail-overs.

```bash
P=docs/eval/nlu_haiku_20261008/providers_haiku.yaml
uv run python -m eval.nlu_corpus --label CONTEXT_OFF_P54 --corpus tests/nlu_corpus_context.jsonl \
  --no-context --providers $P --profile claude_haiku_nlu
uv run python -m eval.nlu_corpus --label CONTEXT_ON_P54  --corpus tests/nlu_corpus_context.jsonl \
  --providers $P --profile claude_haiku_nlu
uv run python -m eval.nlu_corpus --label HAIKU_P54 --providers $P --profile claude_haiku_nlu
```

These are the commands as run, on the prompt with the context sentence.
After the decision, the flags changed. `--no-context` is gone, because the
default now runs the shipped prompt (no sentence, no turns). `--with-context`
reproduces the `CONTEXT_ON_P54` prompt byte for byte. The
`CONTEXT_OFF_P54` and `HAIKU_P54` prompt (the sentence without turns) is no
longer reproducible, and was never shipped.

Rows `CONTEXT_OFF_P54`, `CONTEXT_ON_P54` and `HAIKU_P54` are in
[`../nlu_corpus.md`](../nlu_corpus.md). Raw answers are in
`../records/nlu_corpus_{context_off,context_on,haiku}_p54.jsonl`. The `git`
field shows `6fee094` because the prompt was uncommitted at run time. It
lands in the Phase 54 commit.

## Main corpus (183 lines, no context): new sentence vs `HAIKU_P43_FIX`

Baseline is `records/nlu_corpus_haiku_p43_fix.jsonl`, rescored with the
current guards (`rescore_guards`; P49 hostility floor). Rescoring changes
hostility only (recall 0.400 → 1.000), so the baseline below is the shipped
reading of that run.

| class | P / R / F1 baseline | P / R / F1 `HAIKU_P54` | ΔF1 |
|---|---|---|---|
| asks_client_private_info | 0.971 / 1.000 / 0.986 | 0.971 / 1.000 / 0.986 | 0 |
| demands_commitment | 0.722 / 1.000 / 0.839 | 0.765 / 1.000 / 0.867 | +0.028 |
| firm | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 / 1.000 | 0 |
| wants_to_end | 0.600 / 1.000 / 0.750 | 0.667 / 1.000 / 0.800 | +0.050 |
| hostility | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 / 1.000 | 0 |
| stance accept | 0.917 / 1.000 / 0.957 | 0.917 / 1.000 / 0.957 | 0 |
| stance reject | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 / 1.000 | 0 |
| stance counter | 0.889 / 0.727 / 0.800 | 0.857 / 0.818 / 0.837 | +0.037 |
| stance info | 0.963 / 0.881 / 0.920 | 0.961 / 0.831 / 0.891 | **−0.029** |
| stance question | 0.936 / 1.000 / 0.967 | 0.957 / 1.000 / 0.978 | +0.011 |
| stance other | 0.857 / 0.686 / 0.762 | 0.871 / 0.771 / 0.818 | +0.056 |
| stance stall | 0.333 / 1.000 / 0.500 | 0.333 / 1.000 / 0.500 | 0 |

| metric | baseline | `HAIKU_P54` |
|---|---|---|
| stance accuracy (8 labels) | 0.869 | **0.880** |
| private-info recall | 1.000 | **1.000** |
| term exact-match, all lines | 0.978 | 0.978 |
| term exact-match, lines with terms (66) | 62/66 (miss f16, f17, d04, d11) | 62/66 (miss f16, f17, d04, d07) |
| filler false accepts (31) | 0 | 0 |

**Gate (no class F1 down > 0.03, private-info recall not down): passed.**
The closest call is stance `info` at −0.029 (52 → 49 TP: n17 → stall, c14 →
other, c18 → stall, k01 → counter). Five lines got better (f01 and t13 offer →
counter; i03, f14 and e10 → other), plus flags x02 (wants_to_end FP gone) and
x03 (commitment FP gone). Terms: d11 is fixed and d07 now reads $200 as
ambiguous. A rerun of the same prompt moved about 6 fields in Phase 43, so
most of these per-line changes are within run-to-run noise.

## Context file (16 lines), off vs on

`tests/nlu_corpus_context.jsonl` is new. Each line has 3 earlier turns. The
agent line is the last sentence of the last agent turn, as live. Off and on
use the same prompt; `--no-context` only drops the turns.

| id | kind | rep line | expected | context off | context on |
|---|---|---|---|---|---|
| cx01 | pronoun | Sorry, can you change it to $250? | min $250; counter | min $250 ✓ | min $250 ✓ |
| cx02 | pronoun | Actually, can we do that in eight instead? | max 8; counter | max 8 ✓ | max 8 ✓ |
| cx03 | which is it | $250. | min $250; info | min $250 ✓ | ambiguous $250 ✗ |
| cx04 | which is it | Go with eight. | max 8; info | max 8 ✓ (stance counter) | max 8 ✓ (stance counter) |
| cx05 | bare value | Okay, $250. | min $250; info | ambiguous $250 ✗ (stance other) | ambiguous $250 ✗ |
| cx06 | bare value | It's $175. | min $175; info | ambiguous $175 ✗ | ambiguous $175 ✗ |
| cx07 | stale number | We need even payments. | even; info | even ✓ (fast path) | even ✓ (fast path) |
| cx08 | stale number | The first payment can be on May 1. | May 1; info | May 1 ✓ | May 1 ✓ (stance counter) |
| cx09 | stale number | That's too low for us. | none; reject | none ✓ | none ✓ |
| cx10 | pronoun | Make that six, sorry. | max 6; counter | max 6 ✓ | max 6 ✓ |
| cx11 | pronoun | Actually, switch it to balloon. | balloon; counter | balloon ✓ | balloon ✓ |
| cx12 | pronoun | Can we push it to June 1? | June 1; counter | June 1 ✓ | June 1 ✓ |
| cx13 | control | We allow up to ten payments. | max 10; info | ✓ | ✓ |
| cx14 | control | We could come down to fifty-five percent. | 55%; counter | ✓ | ✓ |
| cx15 | control | What's the client's monthly income? | private; question | ✓ | ✓ |
| cx16 | control | No, that's all, thanks. | end; other | ✓ | ✓ |

| metric | off | on |
|---|---|---|
| term exact-match (16 lines) | 0.875 (14/16) | 0.812 (13/16) |
| stance accuracy | 0.875 | 0.875 |
| stale numbers extracted (cx07–cx09) | 0 | 0 |
| controls changed (cx13–cx16) | — | 0 |
| prompt tokens, mean per call | 2,014 | 2,093 (+79, +3.9%) |
| latency p50 / p95 (15 live calls) | 1,530 / 2,509 ms | 1,529 / 2,865 ms |

What this shows:

- **Context did not help Haiku on this set.** The pronoun lines already pass
  without it, because the agent's last sentence plus the line are enough.
  The bare-value lines still come back `amount_ambiguous` (cx05, cx06), and
  cx03 got worse with context. The earlier "$180" in the turns seems to push
  the model to treat "$250" as a doubtful amount.
- **No harm on stale numbers or controls.** No number from an earlier turn
  was extracted, and the 4 controls read the same both ways. The
  quote-verbatim check would drop a lifted value anyway (unit test
  `test_context_quote_is_not_verified`).
- **The live call's shape is fixed in code, not by the model.** When a term
  is CONTRADICTED, a short reply that names one side ("$250", "the new one")
  is now settled before any model call (decision B). So cx03's regression
  never reaches the agent in that state. The change request (cx01) now
  updates the acked value directly (decision A).
- **cx05 / cx06 stay open.** A bare "$250" after a side remark (not right
  after our ask, and with no clarify open) still triggers the
  total-or-per-payment question. Neither context nor the new rules change
  that.
- Small sample: 15 live calls per side and one run each. The p95 difference
  is one slow call.

## Cost

209 live Haiku calls (15 + 15 + 179; 5 lines were fast paths), 423,141 input
and 42,703 output tokens: **$0.064** at $0.10 / $0.50 per Mtok, inside the
$0.20 cap. Main corpus latency p50 1,504 ms / p95 2,043 ms, mean prompt 2,020
tokens.
