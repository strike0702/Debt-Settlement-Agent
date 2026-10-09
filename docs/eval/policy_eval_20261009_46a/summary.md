# Policy eval after Phase 46a (haggling rep, counter cap 6), 2026-10-09

Offline oracle eval, the CI gate: oracle NLU, template NLG, template creditor
phrasing, 100 scenarios, seed 7, `offline` profile (no network, no keys).

```bash
python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7
```

- BEFORE: `eval_20261009_022811_s7`, main at `d087c5a` (Phase 45): easy rep everywhere, `max_counters` 4.
- AFTER: `eval_20261009_060032_s7`, branch `phase-46a`, run with `.env` moved aside (code defaults:
  `max_counters` 6). `generated_summary.md`, `summary.json` and `run.json` here are that run's files.
- `eval/thresholds.yaml` is unchanged. **Thresholds: PASS** (both runs).

## The seed-7 set changed (on purpose)

Same 100 ids, clients, hidden rules and personas; the main sampler's draws are
untouched. What changed: each scenario now has a haggle style
(`sim/haggle.py`), drawn from its own `Random(f"haggle:{seed}:{index}")`.
75 calls keep the pre-46a **easy** rep. 25 calls haggle: 14 **holders**, 9
**steppers**, 2 **stallers**. For 22 of them the floor and / or opening ask moved:

- Deal calls with a holder or stepper (14): the floor moved from ~22–28% (under
  our first counter, so every deal closed at once) to 80–92% of min(opening
  ask, accept line): above our anchor (70% of the same) and at or below the line.
  Opening asks stayed (each was already more than 6 points above the new floor).
- No-fix calls with a holder or stepper (8): the opening ask moved from floor + 5
  to floor + 10–25 points, so the rep has room to haggle. The floor (above the
  line) did not move.
- Stallers (2 deal calls) keep their floor and ask.

Labels were recomputed after the move and no call changed stratum, `zopa` or
`should_escalate` (`tests/unit/test_sim_haggle.py::test_seed7_haggle_labels_stay_valid`).
Every style still accepts only at or above the floor, so hidden limits never loosen.

## BEFORE vs AFTER

| metric | BEFORE | AFTER | note |
|---|---|---|---|
| deal rate given zopa | 1.000 (23/23) | **0.913 (21/23)** | the 2 misses are the stallers, handed off by the loop guard (by design) |
| agreement_valid | 1.000 (23) | 1.000 (21) | |
| no_deal_correct | 1.000 (22) | 1.000 (22) | 2 no-fix calls now end `max_counters` (a fitting reason) |
| escalation_correct | 1.000 (77) | 1.000 (77) | |
| surplus_captured (mean) | 0.689 (23) | 0.883 (21) | not comparable: 14 deal floors moved up, and surplus is measured from the floor |
| counters spoken mean / max | 0.44 / 2 | **1.32 / 6** | cap is now 6 (`counters_spoken_max <= max_counters` passes) |
| turns_to_outcome (mean) | 4.61 | 5.44 | 461 → 544 rep turns |
| turns_to_proposal (mean) | 3.96 | 6.52 | haggled deals take longer to reach a schedule |
| rule_extraction_accuracy | 0.670 | 0.661 | two stalled deals hand off before the late read-back |
| false_known_rate | 0.000 | 0.000 | |
| sensitive_leaks / unverified | 0 / 0 | 0 / 0 | |
| stuck calls / identical moves | 0 / 0 | 0 / 0 | |

Final reasons, AFTER: deal `confirmed` 21, `no_progress` 1, `repeated_question` 1;
no_fix `above_accept_line` 16 (BEFORE 18), `max_counters` 2 (BEFORE 0),
`infeasible` 4; rescue `out_of_guardrail` 22; `sensitive_request` 33 (pressuring, unchanged).

## Price ladder branches (agent moves, from the audit)

Counted from each call's `policy/decide` audit events (`eval.metrics.ladder_branches`).
A `COUNTER` with reason `bp=…` is the anchor when it is the call's first counter,
else a half-move concession (or a re-anchor right after a yes to a term change).

| branch | BEFORE | AFTER |
|---|---|---|
| anchor (first counter) | 41 | 40 |
| hold (restate our offer) | 0 | **30** |
| quarter step | 0 | **11** |
| concede half of the rep's move | 3 | **41** |
| re-anchor after a term change | 0 | 0 |
| firm → final counter | 0 | **10** |
| rep accepted our counter | 23 | 9 |
| accept on repeat after final counter (`rep_firm`) | 0 | **10** |
| accept after hold + steps (`rep_held`) | 0 | 0 |
| cap reached, accept (`counters_exhausted`) | 0 | **2** |
| firm above the line → handoff (`above_accept_line`) | 18 | 16 |
| cap reached, above line → handoff (`max_counters`) | 0 | **2** |
| loop guard: `repeated_question` | 0 | **1** |
| loop guard: `no_progress` | 0 | **1** |
| loop guard: `max_turns` | 0 | 0 |

Every branch the phase asked for fires at least once. Not exercised: `rep_held`
(a holder long enough to sit through our hold and both steps usually meets a
step at or above its floor and accepts it; tried 3-turn holds, still 0) and
`max_turns` (the loop guard ends stalls long before 24 turns). Both stay covered
by `tests/unit/test_policy.py`.

## Sim LLM rewrite check

Not exercised here (template phrasing: 0 attempts, rate null). Live smoke on the
free `sim` route, `--nlu oracle --nlg template --sim-phrasing llm --profile eval
--scenarios 5 --seed 7` (`eval_20261009_055431_s7`, thresholds PASS): **16 of 28
rewrites dropped for the draft (0.571): `empty` 8, `stance` 5, `figures` 3.**
`empty` is the provider returning no text (a Cerebras `gpt-oss-120b` failover
spends its 120 tokens on reasoning). Stance drops seen in a logged re-run were
real contradictions, e.g. draft "The maximum is 5 payments." → "Yes, the maximum
is 5 payments, but actually it is not."

## Transcripts

- [`s0007_000_deal_flexible`](s0007_000_deal_flexible.md): holder; anchor, hold, quarter step, firm, final counter, accept on repeat.
- [`s0007_084_no_fix_flexible`](s0007_084_no_fix_flexible.md): stepper above the line; concessions to the six-counter cap, `max_counters` handoff.
- [`s0007_010_deal_contradictory`](s0007_010_deal_contradictory.md): staller that never names a number; `repeated_question` handoff.
- [`s0007_024_deal_flexible`](s0007_024_deal_flexible.md): staller on counters; hold, two steps, `no_progress` handoff.
