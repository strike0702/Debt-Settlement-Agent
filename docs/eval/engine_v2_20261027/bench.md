# Engine v2 affordability-scan timing: before and after the exact search (Phase 57b)

Command (same machine, Apple M5, one process, 2026-10-27):

    uv run python scripts/engine_bench.py --repeat 15 --adversarial 300    # before and after
    uv run python scripts/engine_bench.py --repeat 15 --adversarial 2000   # after only

One scan is what `engine_adapter.affordability` does on a cache miss: `build_problem` once,
then `engine.evaluate` for bp 100, 200, …, 10000. **Before** is a `git archive` export of
`8e7ab2e` (main plus the plan revision; it enumerates and scores the take-home family).
**After** is this branch (`feasibility/search.py`: branch and bound over the pinned
family, plus box vectors for staircases with three or more levels). The engine output is
the same on every golden case.

## Fixed targets (median of 15 cold scans, ms)

| target | before | after |
|---|---|---|
| demo, even rules (`fixtures/demo`, 8 payments, min $50) | 3.61 | 1.90 |
| demo, 4-level staircase, tier $90 from payment 5 | 49.96 | 3.57 |
| balloon_structure | 14.69 | 3.05 |
| counter_ladder | 3.00 | 2.02 |
| easy_deal | 2.99 | 2.01 |
| haggling_rep | 4.01 | 2.32 |
| late_start_date | 0.26 | 0.37 |
| no_space | 2.04 | 3.02 |
| rescue_escalate | 9.01 | 2.90 |

The slowest single run of any fixed target after the change is 3.84 ms. Two small
scenarios got slower: late_start_date by 0.1 ms and no_space by 1 ms. The search has a
fixed per-bp cost (bounds per k, cached tails) that the old loop did not have when the
family was tiny. Both are far inside the budget.

## Case sets (one cold scan per case, ms)

| set | n | before p50 / p95 / max | after p50 / p95 / max | after > 150 ms |
|---|---|---|---|---|
| 5 engine fixtures | 5 | 11.05 / 92.34 / 92.34 | 2.72 / 5.66 / 5.66 | 0 |
| golden random (`random_cases(55, 600)`) | 600 | 8.21 / 736.5 / 10,753 | 3.08 / 17.27 / 78.1 | 0 |
| adversarial (`adversarial_cases(57, 300)`) | 300 | 238.0 / 2,854 / 9,158 | 14.65 / 63.28 / 124.0 | 0 |
| adversarial (`adversarial_cases(57, 2000)`) | 2000 | not run | 14.76 / 66.37 / 219.6 | 6 |

Slowest golden case after: `r55_0224` (78 ms; 4 segments, 21 payments). Before, the same
kind of case took 5.7–10.8 s (`r55_0317`: 10.75 s), because the old path generated and
fully simulated about 7,000 candidates per bp.

## Against the budget

- **p50 ≤ 2× the Phase 55 figure.** Phase 55 recorded the demo scan at 6.7 ms on its
  machine. On this machine, the even-rules demo scan is 3.61 ms before and 1.90 ms after
  (0.53×).
- **Max ≤ 150 ms per 100-point scan** on the 7 scenarios, the demo and the golden
  full-size cases: met. The worst is 78.1 ms (golden random); the scenarios and demo
  stay under 4 ms.
- **Adversarial.** 6 of 2,000 adversarial scans are over 150 ms (max 219.6 ms), recorded
  as allowed. The family is not trimmed. The previous engine took up to 9.2 s on these.

## What made it fast

- Caps are reused across the scan: `Problem.window_caps` and `Problem.window_items` are
  cached on the problem, and each bp only builds the tail windows (k..M) once per k.
- Objectives come from prefix sums only, with no balance dict per vector. The schedule
  winner is simulated once, at the end.
- Branch and bound on the monotone prefix of `score_key` (Φ, −k, min slack, total slack)
  for Part 1, and on L and X for Part 2. Whole k are skipped from floor-sum bounds. Within
  a composition only the smallest closed-form level can win, so the old scan over
  `v_{m-1}` is gone.
- The box stage (lifted early levels) runs only for plain staircases with ≥ 3
  segments. It is bounded by "strictly better than the pinned stage", so it usually
  stops at the k level.
- Memoising per (k, composition) across bp was not needed to meet the budget, so it was
  not added.

## Not in the budget: many segments

The generators draw 1–4 segments. With more segments the composition tree grows
combinatorially. On the slowest of 60 adversarial cases (k = 18), forcing
`max_segments` gave these scan times:

| segments | engine scan | legacy scan (estimated from 10 bp) |
|---|---|---|
| 4 | 95 ms | — |
| 6 | 878 ms | ~34 s |
| 8 | 4.4 s | ~117 s |

The engine is 30–40× faster than before on these, but above budget. The scenarios use 2–3
segments. Recorded as an open issue in PROGRESS.
