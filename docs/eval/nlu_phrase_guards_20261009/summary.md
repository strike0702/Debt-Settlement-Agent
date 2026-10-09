# Phrase guards for firm / accept / reject (Phase 45, items D and D2), 2026-10-09

**Change.** `app/agent/nlu.py`: a phrase from the firm list (`_FIRM_RE`), the
accept list or the reject list no longer forces the flag or the stance when it
sits inside a question ("Is that your final offer?", "Is that agreed?"), after a
conditional earlier in its clause ("if", "whether", "unless", "before",
"until", "once") or after a negator in its clause ("not the lowest we can go",
"That's not too low"). The LLM's own reading stands for those lines. Firm stays
"LLM flag OR phrase list"; the phrase list itself is unchanged (no new synonyms).
Ledger items 31.1 and 31.2 (probes in
[`nlu_guard_20261008/probes.jsonl`](../nlu_guard_20261008/probes.jsonl)).

**Model.** Claude Haiku 5.5, effort `low`, eval-only providers file
[`nlu_haiku_20261008/providers_haiku.yaml`](../nlu_haiku_20261008/providers_haiku.yaml)
(profile `claude_haiku_nlu`, one target, no fallback), the shipped P43 NLU prompt.

## How it was measured

1. Committed 20 new hand-labelled lines first: `tests/nlu_corpus_firm.jsonl`
   (10 firm synonyms such as "as low as we'll go", "where we land", "won't
   budge"; 10 near-misses: questions, negations, hypotheticals). None copies a
   corpus line.
2. One paid Haiku pass on the firm lines (`FIRM_HAIKU_P45`) and one on the 12
   probes (`PROBES_HAIKU_P45`), both on the *old* guards. The runner now also
   saves the raw LLM `firm` flag (`predicted.firm_raw`) and `has_terms`.
3. Changed the guards, then rescored every saved record with
   `python -m eval.nlu_corpus --from-records <jsonl> --rescore-guards`, which
   re-runs `repair_stance(stance_raw, …)` and `repair_firm(firm_raw, …)` with the
   current code and makes no LLM call. Rescoring the old records with the old code
   reproduced every saved stance exactly (183 + 32 + 12 lines, 0 differences).
   Rows: `*_P45_GUARD` in [`nlu_corpus.md`](../nlu_corpus.md).

## Results

| set | lines | stance accuracy, old guards | stance accuracy, new guards | firm TP / FP / FN, old → new |
|---|---|---|---|---|
| main corpus (`HAIKU_P43_FIX` records) | 183 | 0.869 | 0.869 (0 lines changed) | 6 / 0 / 0 → unchanged* |
| held-out stance (`HELDOUT_STANCE_HAIKU_P43_FIX`) | 32 | 0.969 | 0.969 (0 lines changed) | none labelled |
| firm lines (new) | 20 | 0.950 | 0.950 | 10 / **8** / 0 → 10 / **0** / 0 |
| probes 31.1 / 31.2 | 12 | **0.167** (2/12) | **0.833** (10/12) | none labelled |

\* Main and held-out records predate `firm_raw`, so their firm flag was not
rescored; instead every corpus line in `tests/nlu_corpus*.jsonl` was checked:
no line where the old phrase list fired is blocked by the new guard.

- **Firm:** Haiku's own flag was 10/10 with no false positives on the new
  lines. The phrase list alone added 8 false positives (fn01, fn02, fn04–fn07,
  fn09, fn10: "Is that your final offer?", "isn't our final number", "not the
  lowest we can go", "wouldn't call sixty percent our floor", …). With the guard:
  precision 0.556 → 1.000, recall 1.000 both ways.
- **Stance probes:** the 6 accept-phrase lines inside a question or conditional
  (q01–q06) and the 2 negated or self-corrected reject lines (q08, q10) now keep
  Haiku's correct label. Still wrong: q07 "That's not too low." (labelled
  accept, Haiku says reject; the guard no longer forces anything, so this is the
  model) and q09 "The problem isn't that it's too low, it's the start date."
  (labelled info, Haiku says reject).
- **Corpus:** no change on either set, so the guard costs nothing on the lines
  it was tuned on.

## Cost

Haiku 5.5 at $0.10 / $0.50 per million input / output tokens: 32 live calls,
63,651 input and 5,757 output tokens, **$0.0092** (cap for the phase: $0.10).
Audit: `eval/results/nlu_corpus_audit_p45.db` (git-ignored).

## Commands

```bash
python -m eval.nlu_corpus --label FIRM_HAIKU_P45 --corpus tests/nlu_corpus_firm.jsonl \
  --profile claude_haiku_nlu --providers docs/eval/nlu_haiku_20261008/providers_haiku.yaml \
  --concurrency 2 --audit-db eval/results/nlu_corpus_audit_p45.db
python -m eval.nlu_corpus --label PROBES_HAIKU_P45 --corpus docs/eval/nlu_guard_20261008/probes.jsonl \
  --profile claude_haiku_nlu --providers docs/eval/nlu_haiku_20261008/providers_haiku.yaml \
  --concurrency 2 --audit-db eval/results/nlu_corpus_audit_p45.db
# after the guard change, no LLM calls:
python -m eval.nlu_corpus --label HAIKU_P45_GUARD --from-records docs/eval/nlu_corpus_haiku_p43_fix.jsonl \
  --rescore-guards --profile claude_haiku_nlu
#   … and the same for nlu_corpus_heldout_stance_haiku_p43_fix, nlu_corpus_firm_haiku_p45,
#   nlu_corpus_probes_haiku_p45 (labels HELDOUT_STANCE_HAIKU_P45_GUARD, FIRM_HAIKU_P45_GUARD,
#   PROBES_HAIKU_P45_GUARD)
```
