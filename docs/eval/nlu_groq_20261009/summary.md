# Phase 44: Groq check of the P43 NLU prompt (2026-10-09)

Measurement only. `app/`, `config/providers.yaml`, prompts, NLU rules, policy,
reason codes and thresholds are unchanged. No fix lands here.

## Question

The demo NLU route is Claude Haiku 5.5 (budgeted) first, then
`groq/openai/gpt-oss-120b`. Phase 43 changed the NLU prompt for Haiku. Is Groq,
the first free fallback, still acceptable on that prompt?

## Setup

- Prompt: current `main` (`6ff7d1d`), the P43 prompt (`app/llm/prompts.py` from
  `2d0f851`). `app/agent/nlu.py` is as on `main`. Phase 45 (in parallel, not on
  `main` yet) adds a question / negation guard to the stance phrase rules; it is
  **not** measured here.
- Providers: `docs/eval/nlu_groq_20261009/providers_groq.yaml`, profile
  `groq_nlu`, NLU route `groq/openai/gpt-oss-120b` only (the demo route's Groq
  entry: no params, no `timeout_s`, so the 6 s NLU role timeout applies). No
  fallback, no Anthropic provider, `--allow-budgeted` not passed. Key pool
  `GROQ_API_KEY_1`..`_4` (four orgs); answers spread 46 / 45 / 46 / 42 on the
  main run.
- Commands:

  ```
  P=docs/eval/nlu_groq_20261009/providers_groq.yaml
  uv run python -m eval.nlu_corpus --label GROQ_P44 --providers $P --profile groq_nlu
  uv run python -m eval.nlu_corpus --label AMOUNTS_GROQ_P44 --corpus tests/nlu_corpus_amounts.jsonl --providers $P --profile groq_nlu
  uv run python -m eval.nlu_corpus --label HELDOUT_STANCE_GROQ_P44 --corpus tests/nlu_corpus_heldout_stance.jsonl --providers $P --profile groq_nlu
  uv run python -m eval.nlu_corpus --label GROQ_P44_NO_GUARD --from-records docs/eval/nlu_corpus_groq_p44.jsonl --no-repair-stance
  uv run python -m eval.nlu_guard_report docs/eval/nlu_corpus_groq_p44.jsonl
  ```

- Every row is one model (`groq/openai/gpt-oss-120b`, plus 4 fast-path lines on
  the main corpus). The first main pass ended at 180/183 (f15, t03, x04 skipped
  on per-minute 429s and 6 s timeouts); a rerun of the same label replayed the
  180 cached replies and answered the 3 gaps. The daily token cap was never hit.
- Cerebras (`CEREBRAS_P44`, optional) was **not run**: there is no
  `CEREBRAS_API_KEY` in this worktree's `.env`.
- Cost: $0 (free tier). 225 live calls, 323,409 input / 124,819 output tokens.

## Comparison rows

- `FILLER_BEFORE` (Phase 28, 2026-10-07, git `9998a65`): the latest Groq
  `gpt-oss-120b` main-corpus row. Its prompt is the **pre-Phase 39** NLU prompt:
  no dollar-total / ambiguous-amount fields (P39) and none of the P42/P43 stance
  and private-info guidance. Its `nlu.py` also predates the P31 negation guard
  and the P39 amount checks. So it differs from GROQ_P44 in prompt and repair
  code, and by two days of Groq drift.
- `HAIKU_P43_FIX` (Phase 43): Claude Haiku 5.5 on the same P43 prompt, the
  demo's primary.

## Main corpus (183 lines)

Per-class precision / recall / F1 (repaired stance, as shipped):

| class | pos | GROQ_P44 | FILLER_BEFORE (Groq, pre-P39 prompt) | HAIKU_P43_FIX |
|---|---|---|---|---|
| asks_client_private_info | 34 | 1.000 / 1.000 / **1.000** | 0.966 / 0.824 / 0.889 | 0.971 / 1.000 / 0.986 |
| demands_commitment | 13 | 0.917 / 0.846 / 0.880 | 0.857 / 0.923 / 0.889 | 0.722 / 1.000 / 0.839 |
| firm | 6 | 0.667 / 1.000 / 0.800 | 0.667 / 1.000 / 0.800 | 1.000 / 1.000 / 1.000 |
| wants_to_end | 6 | 0.462 / 1.000 / 0.632 | 0.500 / 1.000 / 0.667 | 0.600 / 1.000 / 0.750 |
| hostility | 5 | 1.000 / 0.200 / **0.333** | 1.000 / 0.600 / 0.750 | 1.000 / 0.400 / 0.571 |
| stance=counter | 22 | 0.870 / 0.909 / 0.889 | 1.000 / 0.227 / 0.370 | 0.889 / 0.727 / 0.800 |
| stance=accept | 11 | 0.917 / 1.000 / 0.957 | 1.000 / 1.000 / 1.000 | 0.917 / 1.000 / 0.957 |
| stance=reject | 9 | 1.000 / 0.889 / 0.941 | 1.000 / 0.889 / 0.941 | 1.000 / 1.000 / 1.000 |
| stance=stall | 3 | 0.750 / 1.000 / 0.857 | 0.600 / 1.000 / 0.750 | 0.333 / 1.000 / 0.500 |
| stance=info | 59 | 0.851 / 0.966 / 0.905 | 0.735 / 0.610 / 0.667 | 0.963 / 0.881 / 0.920 |
| stance=question | 44 | 0.930 / 0.909 / 0.920 | 0.950 / 0.864 / 0.905 | 0.936 / 1.000 / 0.967 |
| stance=other | 35 | 0.885 / 0.657 / 0.754 | 0.815 / 0.629 / 0.710 | 0.857 / 0.686 / 0.762 |

(`stance=offer` has no positives in the main corpus; GROQ_P44 predicts it on no
line, FILLER_BEFORE on 38.)

| metric | GROQ_P44 | FILLER_BEFORE | HAIKU_P43_FIX |
|---|---|---|---|
| stance accuracy (8 labels) | **0.885** | 0.672 | 0.869 |
| private-info recall | **1.000** (34/34, 0 FP) | 0.824 | 1.000 (1 FP, n10) |
| term exact-match, lines with terms (n=66) | 62/66 | 61/66 | 62/66 |
| term exact-match, all lines | 0.973 | 0.967 | 0.978 |
| filler false accepts (n=31) | **1** (f23) | 0 | 0 |

Term misses: GROQ_P44 f01, f16, f17, d04 (ask / count / cents-ambiguity not
extracted) and k05 (`max_payments: 1` on a line with no terms). FILLER_BEFORE
missed the same five plus t11; Haiku missed f16, f17, d04, d11.

### Classes whose F1 drops by more than 0.03 vs FILLER_BEFORE

| class | FILLER_BEFORE → GROQ_P44 | lines |
|---|---|---|
| hostility | 0.750 → 0.333 | FN x01 "This is a waste of my time.", x02, x03 "Shut up and give me a real number.", x04 (FILLER_BEFORE missed x02, x04). New misses: x01, x03 |
| wants_to_end | 0.667 → 0.632 | FP c06, i07, x02, e06, e07, e10 (same as before) + **c05** "Promise me this is a done deal and I'll close the file." |
| stance=accept | 1.000 → 0.957 | FP **f23** "fine whatever just make it eight payments" (label counter; raw LLM said accept, no rule fired). f23 was also a filler false accept in `DEFAULT_EFFORT` (Phase 21) |

No other class dropped by more than 0.03. 65 lines changed stance vs
FILLER_BEFORE, almost all `offer` → `counter` / `info` toward the label (the
P42/P43 stance guidance), plus a10 "That's non-negotiable." reject → info
(now a reject FN) and x08 other → reject (fixed).

For reference, vs HAIKU_P43_FIX the drops > 0.03 are firm (1.000 → 0.800; FP
k05, t12, i10), wants_to_end (0.750 → 0.632; extra FP c05, c06, i07), hostility
(0.571 → 0.333; extra FN x03), reject (1.000 → 0.941; FN a10) and question
(0.967 → 0.920; FN p07, n02, p14, p17; FP i03, n08, i08).

### Stance guard on vs off (ledger 37.1; no new calls)

`GROQ_P44_NO_GUARD` rescores the saved GROQ_P44 records with
`--no-repair-stance`. This is current `main` (before Phase 45's guard change).

| stance | F1 guard on | F1 guard off |
|---|---|---|
| counter | 0.889 | 0.889 |
| accept | **0.957** (P 0.917, R 1.000) | 0.762 (P 0.800, R 0.727) |
| reject | 0.941 | 0.941 |
| stall | 0.857 | 0.857 |
| info | 0.905 | 0.905 |
| question | 0.920 | 0.920 |
| other | **0.754** | 0.698 |
| **stance accuracy** | **0.885** | 0.863 |

Filler false accepts: 1 both ways (f23). The guard changed 4 lines, all helped:
i02 (injection line, raw accept → other) and f10 / f19 / f20 ("okay", "ok",
"cool"; short-ack rule, raw other → accept). The `accept_phrase` rule decided 5
lines and the `reject_phrase` rule 3, and changed none of them (the raw Groq
label already agreed). So on Groq the phrase-rule override itself moved
nothing; the gain is from the short-ack and injection rules.

## Held-out stance set (32 lines)

| metric | HELDOUT_STANCE_GROQ_P44 | HELDOUT_STANCE_HAIKU_P43_FIX |
|---|---|---|
| stance accuracy | 0.969 | 0.969 |
| private-info recall | **0.833** (FN hs28 "How much does your firm collect from them monthly?") | 1.000 |
| wants_to_end recall | 1.000 | 0.800 (FN hs20) |
| term exact-match, lines with terms (n=16) | 14/16 (hs08 date + even not extracted; hs09 `max_token_pays: 2` instead of `max_segments: 2`) | 15/16 (hs08) |

Stance miss: hs15 "Push the start to June 10 instead." counter → info. There is
no earlier Groq row on this set.

## Amounts set (14 lines)

Terms 12/14 (`AMOUNTS_HAIKU_P43_FIX` 14/14). The two misses are the
ambiguous-amount lines:

- **am09, the user's sentence** ("You must pay $420 by March 31 to avoid
  further action. We only accept 3 even payments, so settle this now."): Groq
  returns `settlement_ask_total_cents` $420 + 3 even payments, **not**
  `amount_ambiguous`. So Groq still does not flag it as ambiguous. A verified
  total goes straight to an ask in basis points with no question (Phase 39: on
  the demo case's $1,250 balance that is a 33.6% ask); the `total_cue` and
  `min_exceeds_balance` question triggers apply only when $420 is filed as a
  minimum. Same reading as `AMOUNTS_P39` (Groq, first P39 wording). Haiku on the
  same prompt flags it ambiguous.
- am10 ("We would need $600 from the client."): also read as a $600 total
  instead of ambiguous, as on `AMOUNTS_P39`.

Stance accuracy 0.357 is not comparable between runs: the amounts set labels
asks `offer` where the main corpus uses `counter` (P43 open issue c).

## Latency (audit `latency_ms`, successful live calls)

| run | calls | p50 | p95 | max |
|---|---|---|---|---|
| main | 179 | 1604 ms | 2179 ms | 2718 ms |
| amounts | 14 | 1703 ms | 2555 ms | 2598 ms |
| held-out | 32 | 1510 ms | 1950 ms | 2032 ms |
| all | 225 | 1594 ms | 2177 ms | 2718 ms |

Mean tokens per call: 1,437 input / 556 output (about 2.0K, so a full main pass
is about 357K tokens, above the 230K the `nlu_corpus.md` header quotes for the
older prompt). Haiku P43 probe: p50 1172 ms / p95 1805 ms.

Failed attempts: 19 per-minute 429s and 36 timeouts (6 s). Every timeout had
spent at least 4.3 s queued in the client's limiter or key cooldown after TPM
429s, so they come from running corpus lines back to back on four 8K TPM keys,
not from slow Groq replies (no successful call took more than 2.8 s of request
time). A demo call makes one NLU call per rep turn.

## Conclusion (facts and options, for the user)

On the P43 prompt, Groq `gpt-oss-120b`:

- matches or beats the demo primary on the headline numbers: stance accuracy
  0.885 (Haiku 0.869, old Groq row 0.672), private-info recall 1.000 on the
  main corpus, terms 62/66 (same as Haiku), p50 1.6 s / p95 2.2 s, well inside
  the 6 s NLU timeout;
- is weaker than Haiku on: hostility (1 of 5 hostile lines flagged, Haiku 2,
  old Groq row 3), wants_to_end false positives (7, Haiku 4), firm false
  positives (3, Haiku 0), one filler false accept (f23), held-out private-info
  recall (hs28 missed), and **the user's $420 sentence, which Groq reads as a
  total ask rather than asking "total or per payment?"**.

Options:

1. Keep Groq as the first free fallback unchanged. Its weak spots only apply
   when Haiku is skipped (no key, budget used, or a timeout).
2. Keep Groq, and make a verified dollar total in a sentence that also gives an
   exact payment count (am09's shape) ask the question in code, so am09 no
   longer depends on the model. That is a policy / NLU rule change for a later
   phase.
3. Measure Cerebras `gpt-oss-120b` (the second fallback) on the same prompt
   once a `CEREBRAS_API_KEY` is available, as a separate row.
4. Re-run GROQ_P44 another day before deciding: P39 and P28 showed run-to-run
   movement of several lines on Groq (hostility and private-info especially).
