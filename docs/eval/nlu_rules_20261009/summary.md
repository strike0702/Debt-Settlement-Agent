# Code-side reading rules (Phase 49), 2026-10-09

The question: do four rules in code, run after whatever the NLU model returned,
help every model without hurting any? The rules are user decisions of
2026-10-09, ledger items 44b.1, 44b.2, 44b.4 / 46b.3 and 44b.5.

**Answer: yes, on every saved run.** Hostility recall goes to 1.0 for Haiku,
Groq and Cerebras. Cerebras private-info recall goes from 0.882 to 1.0.
Groq and Cerebras now ask "total or per payment?" on am10, and Cerebras no
longer asks it about n06's balance statement. No line got worse on any model
or set, and no new false positive appeared. **$0: no LLM call was made.**

## The rules

All four are in `app/agent/nlu.py`. Each one only adds a flag or a question,
except rule (c), which drops a question. When a rule does not fire, the
model's reading stands.

| rule | fires on | does not fire on |
|---|---|---|
| (a) private finance (`_PRIVATE_INFO_RE`) | money in the client's escrow / program / settlement / dedicated / savings account; "the most they / your client could pay"; SSN or "the last four of the client's social" | balance owed, original balance, the debt's account number, payment history with the creditor, "the most **we** can do"; disclaimers ("don't tell me what's in their escrow") |
| (b) hostility floor (`hostility_floor_hit`, `HOSTILITY_FLOOR` = 1.0) | insults (idiot, stupid, moron, dumb, clowns, …), "shut up", "waste of my time" / "wasting our time", "report you / your firm", scam accusations | negated ("I'm not calling you stupid"), quoted (`"this is a waste of time"`), hypothetical ("if this were a scam"), firm pushback, plain frustration ("this is taking a while"), "report back to my manager" |
| (c) balance statements (`balance_statement_amount`) | drops the model's `amount_ambiguous` flag when every sentence that says the amount states the debt ("original balance of $6,000", "the client owes $6,000, and …") | a demand in the amount's clause ("We need $2,000 to clear the balance." still asks) |
| (d) bare demanded amount (`bare_amount_ask`, trigger `bare_amount`) | one dollar figure in the sentence, a demand verb before it and a deal target ("from the client", "to close this"): "We would need $600 from the client." | "in total" / "altogether" / "each" / "per payment"; a count, date or % in the sentence; balance statements; "the $2,000 you offered"; "more than $600"; questions; negated; "We'd need $150." (no target, so a short answer to our minimum-payment question is left alone); right after the rep answered the amount question |

## Method (no LLM calls)

1. **Held-out first.** `tests/nlu_corpus_rules_heldout.jsonl` has 36 lines:
   positives and near-misses for each rule, new wording, none copied from the
   other corpora. It was committed (`4514600`) before any rule changed and was
   not tuned on.
2. **Rescore saved runs.** `--from-records … --rescore-guards` now also
   re-runs the private-info cue and the hostility floor. The raw model flags
   were not saved, so the rescore ORs the saved (already repaired) flag with
   the rule. That is exact here, because both rules only add.
   `--amount-view` replays `resolve_amounts` on the saved terms and scores
   what the agent does: an amount it would ask about scores as
   `amount_ambiguous`. The view uses a huge balance so that no balance-based
   trigger fires, since the corpus has no balance. The before columns below
   are the same view with rules (c) and (d) patched out, so they isolate
   Phase 49 from the Phase 46b shape rule.
3. **Rules alone.** `--rules-only` runs every line through `post_verify` with
   an empty analysis (every flag off, no terms). This gives the rules' own
   precision and recall, like `REGEX_ONLY_P32`.

Rows: `docs/eval/nlu_corpus.md` sections `*_P49`; per-line records in
`docs/eval/nlu_corpus_*_p49.jsonl`.

## Main corpus (183 lines), before → after

| model (saved run) | private-info P / R | hostility P / R | lines fixed | new FP |
|---|---|---|---|---|
| Haiku 5.5 `HAIKU_P43_FIX` | 0.971 / 1.000 → same | 1.000 / 0.400 → **1.000 / 1.000** | x01, x02, x04 | none |
| Haiku 5.5 `HAIKU_P45_GUARD` | 0.971 / 1.000 → same | 1.000 / 0.400 → **1.000 / 1.000** | x01, x02, x04 | none |
| Groq gpt-oss-120b `GROQ_P44` | 1.000 / 1.000 → same | 1.000 / 0.200 → **1.000 / 1.000** | x01–x04 | none |
| Cerebras gpt-oss-120b `CEREBRAS_P44` | 1.000 / 0.882 → **1.000 / 1.000** | n/a / 0.000 → **1.000 / 1.000** | p05, p11, p14, p18, x01–x05 | none |

Haiku's one private-info FP (n10) is the model's own and is unchanged. Stance,
firm, commitment and wants_to_end are identical to the saved rows on every
model. The guards were replayed with current code, and no saved stance changed.

## Amounts: am09 / am10 / n06

"asked" means the agent says "Just to be sure: is $X the total settlement, or
the minimum for each payment?".

| model | am09 ($420 pay-by + 3 even payments) | am10 ($600 from the client) | n06 (original balance $6,000) |
|---|---|---|---|
| Haiku `AMOUNTS_HAIKU_P43_FIX` | asked (model flag) → same | asked (model flag) → same | not asked → same |
| Groq `AMOUNTS_GROQ_P44` | asked (`total_pay_by`, P46b) → same | **not asked → asked (`bare_amount`)** | not asked → same |
| Cerebras `AMOUNTS_CEREBRAS_P44` | asked (`total_pay_by`, P46b) → same | **not asked → asked (`bare_amount`)** | **asked (model flag) → not asked** |

The amount view now scores 14/14 on the amounts set for all three models; the
saved NLU-only rows score 14 / 12 / 12. On the main corpus, n06 for Cerebras is
the only amount change: it was wrong and is now right.

## Held-out stance set (32 lines)

| model | private-info P / R before → after |
|---|---|
| Haiku `HELDOUT_STANCE_HAIKU_P45_GUARD` | 1.000 / 1.000 → same |
| Groq `HELDOUT_STANCE_GROQ_P44` | 1.000 / 0.833 → same (FN hs28) |
| Cerebras `HELDOUT_STANCE_CEREBRAS_P44` | 1.000 / 0.833 → same (FN hs28) |

The set has no hostile lines. hs28 ("How much does your firm collect from them
monthly?") is a draft-amount shape, not one of this phase's rules. It is still
missed.

## Rules alone

| set | private-info | hostility | bare amount (d) |
|---|---|---|---|
| **new held-out** (`RULES_HELDOUT_P49`, 36) | 7/7, 0 FP of 7 near-misses | 6/6, 0 FP of 7 near-misses | 2/3, 0 FP of 6 near-misses; **miss hr29** |
| main corpus, P32 held-out, held-out stance, amounts, firm (all labelled lines) | 0 FP | 0 FP; corpus x01–x05 all hit | fires only on am10 |

The private-info rule alone does not reach high recall (most private asks are
paraphrases the model has to read). It only has to catch the shapes the
models missed, and it never fired on a near-miss.

**hr29 miss:** "We're looking for $750 on this one." The number finder reads
"one" in "this one" as a second figure, so the sentence fails the
one-figure test. It was left as is so the held-out set is not tuned on
(DEFERRED below).

## Changes to existing tests

- `test_acks_p46b.py::test_explicit_or_plain_total_does_not_ask` encoded am10
  ("We would need $600 from the client.") as no-ask. Rule (d) reverses that by
  user decision, so the case moved to `tests/unit/test_nlu_rules_p49.py`.
- `test_nlu_nlg_llm.py::test_post_verify_keeps_hostility_with_cues` used
  "waste of time you idiot", which is now a floor phrase lifted to 1.0. It now
  checks the kept model score with a non-floor cue ("hostile", "lawsuit").
- `test_nlu_repairs.py`: the Phase 32 strict-xfail case "What's the most the
  client can pay up front?" now passes, so it moved out of the xfail list.

## Open

- hr29, as above.
- The hostility corroboration regex (`_HOSTILITY_RE`, which keeps a model
  score only if a cue appears) did not gain the new floor words. A guarded
  floor phrase ("I'm not calling you stupid") with a high model score therefore
  behaves as before. The rescore could not measure a change there without the
  raw model score.
- One saved run per model. Groq drifts between days.
