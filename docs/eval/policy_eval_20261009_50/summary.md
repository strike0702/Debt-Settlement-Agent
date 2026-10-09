# Policy eval after Phase 50 (equal steps to the limit, call fixes), 2026-10-09

Offline oracle eval, the CI gate: oracle NLU, template NLG, template creditor
phrasing, 100 scenarios, seed 7, `offline` profile (no network, no keys).

```bash
python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7
```

- BEFORE: `eval_20261009_164612_s7`, main at `062d847` (Phases 48 and 49 merged), run from a
  clean export of that commit with no `.env`. Its summary is [`before_summary.json`](before_summary.json).
- AFTER: `eval_20261009_165345_s7`, branch `phase-50`, `.env` moved aside (code defaults).
  [`generated_summary.md`](generated_summary.md), [`summary.json`](summary.json) and
  [`run.json`](run.json) are that run's files.
- `eval/thresholds.yaml` is unchanged. **Thresholds: PASS** (both runs).

## What changed in the policy

When the representative does not move, the agent still holds once (repeats its
offer), but then takes **two equal steps** that split the gap between its held
offer and the lower of their ask and the accept line. The second step lands on
that number itself. After that it accepts if their ask is at or below the line,
and otherwise hands off (`above_accept_line`). Phase 45 took two steps of a
quarter of the remaining gap each, so a call above the line was handed off well
short of the line. In the user's manual call (accept line 66%, ask 80%) the agent
went 46 → 46 → 51 → 54% and handed off; it now goes 46 → 46 → 56 → 66%.

Steps are whole basis points on the engine's feasible grid: the highest
schedulable bp at or below the step, never above the line. The rest of the
ladder (anchor, half-move concession, final counter, cap) is unchanged.

## BEFORE vs AFTER

| metric | BEFORE | AFTER |
|---|---|---|
| agreement_valid | 1.000 (n=21) | 1.000 (n=21) |
| deal rate given zopa | 0.913 (21/23) | 0.913 (21/23) |
| no_deal_correct | 1.000 (n=22) | 1.000 (n=22) |
| escalation_correct | 1.000 (n=77) | 1.000 (n=77) |
| rule_extraction_accuracy | 0.661 (n=700) | 0.661 (n=700) |
| false_known_rate | 0.000 (n=463) | 0.000 (n=463) |
| sensitive_leaks / unverified_figures_spoken / guard_blocks | 0 / 0 / 0 | 0 / 0 / 0 |
| stuck calls | 0 | 0 |
| surplus_captured (deals) | 0.883 | **0.873** |
| turns_to_outcome | 5.44 | **5.38** |
| turns_to_proposal | 6.52 | **6.24** |
| counters spoken mean / max | 1.32 / 6 | **1.26** / 6 |

Only four calls changed moves, all haggling reps:

- `s0007_004_deal_contradictory` and `s0007_016_deal_contradictory` (holders,
  floor 57%): BEFORE 48 → hold 48 → 53 → 55 → hold 55 → 57, deal at 57% after 12
  rep turns. AFTER 48 → hold 48 → 58, deal at 58% after 9 turns. The bigger step
  overshoots the floor by one point (surplus 1.000 → 0.977 on each), which is the
  whole surplus drop above.
- `s0007_024_deal_flexible` (staller on counters): 48 → hold → 53 → 57 became
  48 → hold → 58 → 69 before the same `no_progress` handoff. No deal either way.
- `s0007_078_no_fix_flexible` (holder above the line): steps 25 / 27 / 28 became
  28 / 30 / 31 before the same `max_counters` handoff.

Every other call spoke the same moves. Other line changes are presentation only:
acks now name the payment structure ("…, starting March 31, with even
payments."), and 14 one-payment confirmations now say "1 payment totaling"
instead of "1 payments totaling".

## Price ladder branches (agent moves, from the audit)

Counted by `eval.metrics.ladder_branches`. The step branch is renamed
`quarter_step` → `equal_step` with the rule.

| branch | BEFORE | AFTER |
|---|---|---|
| anchor (first counter) | 40 | 40 |
| hold (restate our offer) | 30 | 28 |
| step after a hold (quarter → equal) | 11 | **9** |
| concede half of the rep's move | 41 | 39 |
| re-anchor after a term change | 0 | 0 |
| firm → final counter | 10 | 10 |
| rep accepted our counter | 9 | 9 |
| accept on repeat after final counter (`rep_firm`) | 10 | 10 |
| accept after hold + steps (`rep_held`) | 0 | 0 |
| cap reached, accept (`counters_exhausted`) | 2 | 2 |
| firm or held above the line → handoff (`above_accept_line`) | 16 | 16 |
| cap reached, above line → handoff (`max_counters`) | 2 | 2 |
| loop guard: `repeated_question` | 1 | 1 |
| loop guard: `no_progress` | 1 | 1 |
| loop guard: `max_turns` | 0 | 0 |

`rep_held` is now close to unreachable: when the rep's ask is at or below the
line, the second step offers exactly their ask, so a rep who then repeats it is
taken as "asking no more than we offered" (`ask_within_offer`). It stays in the
code for grids where the step cannot land on the ask, and is covered by unit
tests.

## The user's manual call, replayed offline

Oracle NLU standing in for Haiku, template NLG, `fixtures/scenarios/no_space`
($800, client ceiling 88%, accept line 66%), `max_counters` 6. The script is in
`tests/unit/test_p50_call_fixes.py` (`replay_manual_call`). Agent lines only
where they differ:

| rep | BEFORE (main) | AFTER (phase-50) |
|---|---|---|
| balloon works | What settlement percentage of the balance are you looking for? | Got it, a balloon schedule it is. What settlement percentage of the balance are you looking for? |
| 80% | That start date does not fit the client's program. Could payment start on March 31 instead? | Could payment start on March 31? |
| no, we can be flexible on other terms though | (stance read as reject) …Could you allow up to 5 payments? and the structure silently became **flexible** (KNOWN). With Haiku's reading in the live call: "Earlier you mentioned balloon, and now I am hearing flexible. Which of those should I use?" | …Could you allow up to 5 payments? (structure stays **balloon**; the word is dropped as `rejected_structure`) |
| no | We can propose 51% of the balance, which is $408. | We can propose 56% of the balance, which is $448. |
| sorry | We can propose 54% of the balance, which is $432. | We can propose 66% of the balance, which is $528. |
| no doesn't work | …Your number is above what I can accept on this call, so a specialist will review it. | (same) |
