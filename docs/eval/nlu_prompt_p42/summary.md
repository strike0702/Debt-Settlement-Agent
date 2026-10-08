# NLU prompt clarity for Claude Haiku 5.5 (Phase 42, 2026-10-08)

**Question.** Can clearer general stance guidance in the shared NLU prompt fix
Haiku 5.5's weak spot from Phase 40 (rule statements heard as offers/counters,
closings heard as stalls, two missed private-info asks) without hurting
Sonnet 5.5, the demo's NLU model?

**Outcome.**
- **Haiku: yes, on both the main corpus and a new held-out set.** Stance
  accuracy 0.601 → 0.896 on the corpus and 0.562 → 0.906 on the held-out set.
  Private-info recall on the corpus 0.941 → 1.000.
- **Sonnet gate: not run, so the prompt change is reverted.** The new prompt
  is about 35% longer, and a 10-line Sonnet pilot cost $0.0056 per call. The
  three Sonnet runs the gate needs would bring the phase to about $1.48,
  over the $1.30 cap; even main + amounts alone projects $1.302. Per the stop
  rule, no full Sonnet run was made.
- `app/llm/prompts.py` is unchanged from `main`. The candidate prompt is kept
  as `nlu_prompt_v4.patch` in this folder (`git apply` clean on this commit),
  ready for a gated rerun once the Sonnet spend is approved.
- **Cost actually spent: $0.278** (Haiku $0.222, Sonnet pilot $0.056).

## How it was measured

- **Haiku:** `docs/eval/nlu_haiku_20261008/providers_haiku.yaml`, profile
  `claude_haiku_nlu` (Haiku 5.5, effort `low`, one target, no fallback).
  Concurrency 2. Every run was single model and 100% answered; the runner's
  mixed-model check passed.
- **Sonnet pilot:** `docs/eval/nlu_guard_20261008/providers_claude.yaml`,
  profile `claude_nlu`, first 10 main-corpus lines (p01–p10), final prompt.
  Like the Phase 40 pilot, its report section and JSONL were not kept. The
  numbers below come from a free replay of the response cache (0 live calls).
- **Held-out set:** `tests/nlu_corpus_heldout_stance.jsonl` (32 synthetic
  lines, ids `hs01`–`hs32`). Committed in `66f2404` before any prompt change
  or further result was seen:
  - 10 `info`: plain rule statements, plus one public fact with no term;
  - 7 `counter`: % / $-total asks, and changes to a proposed term;
  - 5 `other` + `wants_to_end`: closings and impatience;
  - 6 indirect private-finance asks;
  - 4 near-misses: public questions about the debt.

  Schema test: `test_heldout_stance_set_schema`. It follows the main corpus
  convention that a rep's settlement ask in reply to the agent is `counter`.
- **Order:** baseline on the held-out set (`HELDOUT_STANCE_HAIKU_BEFORE`),
  then four prompt versions on the main corpus (`HAIKU_P42_V1`..`V4`), then
  one held-out run on the final version (`HELDOUT_STANCE_HAIKU_AFTER`).
  - The held-out baseline was seen before v1 was written; the step order
    requires that.
  - v1's private-info line names "questions about the debt itself" as
    not private. That category is in the phase prompt, and the baseline's two
    false positives (hs30, hs31) fall in it.
  - No held-out result was looked at between v1 and the final held-out run.
- **Commands:**
  - `python -m eval.nlu_corpus --label HAIKU_P42_V4 --profile claude_haiku_nlu --providers docs/eval/nlu_haiku_20261008/providers_haiku.yaml --concurrency 2 --audit-db eval/results/p42_audit.db`
  - the held-out runs add `--corpus tests/nlu_corpus_heldout_stance.jsonl`.
- **Variance:** one run per version. Phase 39 saw ±9 Sonnet stance lines
  between runs at equal net accuracy, so one- or two-line moves are noise.

## What changed in the prompt (candidate, reverted)

Only the stance block and one new private-info rule. The JSON schema, every
field's semantics and the P39 amount fields are unchanged. No line-specific
strings: the examples were checked against all three corpora (0 hits).

```
stance must be one of: offer, counter, accept, reject, stall, info, question, other.
- info: the rep states a fact, rule or limit (payment count, minimum per payment,
  even/balloon, first payment date, balance, policy), even hedged, and names no
  settlement price. Extracting a term does not make a line an offer or counter.
  Telling the agent to take it to the client, or that there is no rush, is info.
- counter: the rep names a settlement price (a percent of the balance or a dollar
  total to settle), or asks to change a price or term already proposed by either side.
  offer: the same, but only when Agent last said is (opening).
- accept when the rep agrees to a schedule or counter ("agreed", "that works").
- reject when they refuse terms, are unhappy with an offer, or say a schedule
  does not work.
- question: the rep asks a question or requests information. Demands are not questions.
- stall: the rep puts off their own answer (hold on, let me check, I'll ask my
  supervisor, I'm not sure).
- other: closings, goodbyes, impatience to end the call, insults, and fillers or
  unfinished fragments with no content.
  A rep who is ending or wants to end the call is other, never stall.
Examples: "Six installments is our limit." → info; "We'd take fifty-two percent." →
counter; "Give me a minute to check." → stall; "Okay, I'll let you go." → other.
...
asks_client_private_info=true when the rep asks about the client's own money, directly
or indirectly: income, take-home pay, savings, bank or program account balance, assets,
budget, what the client can afford, or how much the client pays, deposits or has
drafted each month ("How much is the client saving toward this each week?").
Not for questions about the debt itself: balance owed, account number, payment
history, or the terms being proposed.
```

The accept and reject lines were already in the prompt; they were reformatted
as bullets, and reject gained "are unhappy with an offer".

### Iterations (main corpus, Haiku)

| version | change | stance acc. | terms exact (183) |
|---|---|---|---|
| shipped (`HAIKU_P40`) | — | 0.601 | 179 |
| v1 | definitions of info / counter / offer / stall / other, examples, private-info rule | 0.863 | 179 |
| v2 | stall = putting off one's own answer; "take it to the client" = info; fillers = other; offer only at (opening) | 0.869 | 179 |
| v3 | defined question; reject covers "unhappy with an offer"; removed "anything else" from other | 0.885 | 180 |
| v4 (final) | question: "Demands are not questions" | 0.896 | 177 |

- v1 fixed the big error (info → offer/counter) but sent 14 lines to `stall`.
- v2 fixed those, but its catch-all `other` took questions and rejects.
- v3 and v4 fixed those.

## Haiku before / after

### Main corpus (183 lines, guard on as shipped)

| class | pos | Haiku P40 F1 | Haiku P42 v4 F1 | Δ | Sonnet P39 F1 (shipped prompt) |
|---|---|---|---|---|---|
| asks_client_private_info | 34 | 0.941 (R 0.941) | 0.971 (R 1.000) | +0.030 | 0.986 (R 1.000) |
| demands_commitment | 13 | 0.867 | 0.839 | −0.028 | 0.929 |
| firm | 6 | 1.000 | 1.000 | 0 | 1.000 |
| wants_to_end | 6 | 0.750 | 0.800 | +0.050 | 0.750 |
| hostility | 5 | 0.571 | 0.571 | 0 | 0.571 |
| stance=counter | 22 | 0.510 | **0.933** | +0.424 | 0.818 |
| stance=accept | 11 | 0.917 | 0.957 | +0.040 | 0.957 |
| stance=reject | 9 | 0.818 | **1.000** | +0.182 | 0.947 |
| stance=stall | 3 | 0.333 | 0.545 | +0.212 | 0.545 |
| stance=info | 59 | 0.364 | **0.891** | +0.527 | 0.808 |
| stance=question | 44 | 0.952 | 0.967 | +0.015 | 0.914 |
| stance=other | 35 | 0.645 | 0.806 | +0.161 | 0.845 |
| **stance accuracy** | 183 | 0.601 | **0.896** | +0.295 | 0.820 |
| term exact-match (66 lines with terms) | | 62 | 60 | −2 | 63 |
| filler false accepts (n=31) | | 1 (f30) | **0** | | 1 (f28) |

- **Private info:** p08 and f25 (the Phase 40 misses) and c10 (FP) are now
  right. n14 ("I won't ask about savings, just tell me your best percentage.")
  is a new FP. Recall 1.000, precision 0.944.
- **Commitment:** one new FP, a11 ("Take it or leave it, sixty-two percent.").
  n08, i09, x03 and k05 are FPs before and after.
- **Terms:**
  - d11 is fixed.
  - d10 ("Minimum's $75.") and t06 ("At most two payment levels.") were dropped
    in v4 only; v1–v3 got both right.
  - i06 (the injection line) moved to `amount_ambiguous` $200, as on Sonnet.
    It flips between versions.
  - f16, f17 and d04 are wrong before and after.
  - A dropped term makes the agent ask again; `post_verify` never lets a wrong
    number through.
- **Remaining stance misses (19):**
  - info → other 5 (n04, n08, n17, c14, x05);
  - other → question 3 (c08, i04, k02);
  - info → stall 3 (c18, h04, f17);
  - other → stall 2 (c12, e09);
  - info → counter 2 (t15, k01);
  - other → info 2 (k03, k04);
  - other → accept 1 (c11);
  - counter → offer 1 (t09).

### Held-out set (32 lines)

| class | pos | before F1 | after F1 |
|---|---|---|---|
| asks_client_private_info | 6 | 0.857 (P 0.750, R 1.000) | 0.909 (P 1.000, R 0.833) |
| wants_to_end | 5 | 1.000 | 1.000 |
| stance=counter | 7 | 0.421 | 0.769 |
| stance=info | 10 | 0.182 | 0.947 |
| stance=question | 10 | 1.000 | 1.000 |
| stance=other | 5 | 0.750 | 1.000 |
| **stance accuracy** | 32 | 0.562 | **0.906** |
| term exact-match | 32 | 31 | 30 |

- **Fixed:**
  - 8 rule statements: hs01–hs07, hs09;
  - 2 closings no longer labelled stall: hs20, hs22;
  - 1 counter that was labelled offer: hs11;
  - 2 private false positives on debt questions: hs30, hs31.
- **New misses:**
  - hs28 ("How much does your firm collect from them monthly?"): private-info false negative;
  - hs09: the `max_segments` term was dropped (stance fixed).
- **Still wrong:**
  - hs13 and hs16 counter → offer;
  - hs08 info → counter (its `even` term was also dropped before and after).
- **Overfitting check:** the held-out gain (+0.344) is as large as the corpus
  gain (+0.295), so the prompt does not look overfit to the corpus.
- **Watch item:** two `max_segments` drops in v4 runs (t06, hs09). That may
  be a real side effect rather than noise; a gated rerun should check it.

## Sonnet before / after

**Not measured. The cost guard stopped it.**

| item | calls | projected cost |
|---|---|---|
| spent before the Sonnet runs (Haiku + Sonnet pilot) | | $0.278 |
| main corpus, remaining lines | 169 | $0.946 |
| amounts corpus | 14 | $0.078 |
| held-out set | 32 | $0.179 |
| **phase total if all ran** | | **$1.48** (cap $1.30) |

- **Pilot cost:** p01–p10, 19,195 in / 1,760 out = $0.0056 per call.
  - Phase 39 measured $0.0047 per call on the same 10 lines.
  - The difference is the input: 1,920 vs 1,396 tokens per call.
- **Pilot labels:** 10/10 private-info recall (same as P39).
  - Stance 9/10: p07 went from other (wrong) to question (right); p10 is
    `other` on both prompts.
  - Ten private-info lines say nothing about the info/counter question the
    gate is about.
- **Gate:** not evaluated. Per the phase rule, the prompt change is reverted.
- **Cost to finish the gate later** (from this branch):
  1. Apply `nlu_prompt_v4.patch`.
  2. Run `AFTER_P42_CLAUDE` + `AMOUNTS_P42_CLAUDE` + the held-out set on `claude_nlu`.
  3. Expected cost: about $1.20 (or about $1.02 for main + amounts only).
  4. The pilot's 10 replies are in this worktree's response cache, which is
     git-ignored and goes away with the worktree.

## Cost actually spent (from the audit, `eval/results/p42_audit.db`)

`prompt_tokens` includes cache read/write, which are 0 here. No call was a
cache hit or an error.

| run | model | live calls | input | output | cost |
|---|---|---|---|---|---|
| HELDOUT_STANCE_HAIKU_BEFORE | Haiku | 32 | 44,741 | 6,091 | $0.0075 |
| HAIKU_P42_V1 | Haiku | 179 | 329,325 | 34,709 | $0.0503 |
| HAIKU_P42_V2 | Haiku | 179 | 338,275 | 34,444 | $0.0510 |
| HAIKU_P42_V3 | Haiku | 180 | 345,502 | 35,485 | $0.0523 |
| HAIKU_P42_V4 | Haiku | 179 | 344,182 | 34,945 | $0.0519 |
| HELDOUT_STANCE_HAIKU_AFTER | Haiku | 32 | 61,509 | 6,035 | $0.0092 |
| Sonnet pilot (10 lines) | Sonnet | 10 | 19,195 | 1,760 | $0.0560 |
| **total** | | 791 | 1,482,729 | 153,469 | **$0.278** |

- Haiku total: $0.222, within the $0.30 Haiku limit.
- V3 made 180 calls for 179 LLM lines: one line took the NLU's second attempt.

## Plain-language comparison for choosing the demo NLU model

**Facts.**

- **With the candidate prompt, Haiku labels rep turns better than Sonnet did on the shipped prompt.**
  - Stance accuracy is 0.896 vs 0.820. Info F1 is 0.89 vs 0.81, counter F1 0.93 vs 0.82.
  - Private-info recall is 1.000 on both. Accept recall is 1.000 on both.
  - Zero filler false accepts, against one for Sonnet.
- **Haiku is still weaker on a few flags.**
  - Commitment F1 is 0.839 vs 0.929 (5 FPs vs 2).
  - Term exact-match on the 66 lines with terms is 60 vs 63, all drops rather than wrong numbers.
- **Sonnet on the candidate prompt is unknown.** It may improve as Haiku did, or it may move. The gate exists for that.
- **The shipped prompt is unchanged.** Until the change is gated on Sonnet and re-landed, the Phase 40 numbers apply: Haiku 0.601, Sonnet 0.820.

**Speed and cost.**

| | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| latency p50 / p95 per NLU call | 1.20 s / 2.03 s (P42 v4, 179 calls) | ≈ 2.1 s p50 (P37); 2.0 s p50 on the 10-call P42 pilot; p95 never measured on a full run |
| cost per NLU call, shipped prompt | $0.00024 (P40) | $0.0047 (P39) |
| cost per NLU call, candidate prompt | $0.00029 | $0.0056 (pilot) |
| 15-turn demo call, NLU only | ≈ $0.004 | ≈ $0.07–0.08 |

**Options (no change made here).**

1. **Approve the Sonnet gate run** (about $1.20) on the candidate prompt.
   - If it passes, the prompt can ship for both models.
   - Haiku then becomes a fair demo candidate: faster, about 19× cheaper,
     better stance than Sonnet today, slightly more commitment false positives.
2. **Keep Sonnet with the shipped prompt** for the demo, as Phase 41 set up. Leave this change parked.
3. **Haiku on the candidate prompt without the Sonnet gate.**
   - Only safe if the demo's NLU route is Haiku only, because the prompt is shared.
   - That breaks the "no ungated shared-prompt change" rule, so it needs your explicit call.
