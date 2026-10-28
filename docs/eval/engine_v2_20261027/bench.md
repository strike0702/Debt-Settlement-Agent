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

---

# Phase 59: objectives, fee policy, feasibility-only scan

Same machine, 2026-10-27, `--repeat 15 --adversarial 300`:

    uv run python scripts/engine_bench.py --scan feasible                      # adapter scan
    uv run python scripts/engine_bench.py                                      # fee_first ranked
    uv run python scripts/engine_bench.py --objective client_first             # client_first ranked
    uv run python scripts/engine_bench.py --objective client_first --fee-policy proportional
    uv run python scripts/engine_bench.py --fee-policy proportional            # fee_first, proportional

**What changed in the scan.** Feasibility does not depend on the objective or the fee
policy (docs/ENGINE.md). So since Phase 59 `engine_adapter.affordability` calls
`engine.is_feasible` per point, which stops at the first feasible vector, instead of a
ranked `evaluate`. The ranked evaluate runs once per spoken schedule (CONFIRM, wrap),
in the configured mode. Both are timed below. A test checks that `is_feasible` equals
`evaluate(...).feasible` on every scenario point, 150 golden and 40 adversarial cases ×
20 bp, under both policies.

## Fixed targets (median of 15 cold 100-point scans, ms)

| target | adapter scan (feasible) | fee_first ranked | client_first ranked | client_first, proportional | fee_first, proportional |
|---|---|---|---|---|---|
| demo, even rules | 0.66 | 2.18 | 5.93 | 6.00 | 3.66 |
| demo, 4-level staircase + tier | 0.76 | 4.01 | 13.68 | 13.18 | 60.84 |
| balloon_structure | 0.77 | 3.53 | 4.35 | 4.34 | 10.96 |
| counter_ladder | 0.71 | 2.48 | 6.27 | 6.36 | 8.34 |
| easy_deal | 0.71 | 2.51 | 6.29 | 6.32 | 8.72 |
| haggling_rep | 0.82 | 2.91 | 6.15 | 6.27 | 5.83 |
| late_start_date | 0.13 | 0.49 | 0.40 | 0.40 | 1.00 |
| no_space | 0.55 | 3.86 | 3.15 | 3.15 | 4.85 |
| rescue_escalate | 0.37 | 3.82 | 2.99 | 2.97 | 6.51 |

The fee_first ranked column is the 57b code path, re-measured in this session (1.15× the
57b demo figure; the machine was busier).

## Case sets: 100-point scans (p50 / p95 / max ms, scans over 150 ms)

| set | adapter scan | fee_first ranked | client_first ranked | client_first, prop. | fee_first, prop. |
|---|---|---|---|---|---|
| 5 engine fixtures | 0.68 / 1.01 / 1.01, 0 | 3.58 / 7.58 / 7.58, 0 | 5.61 / 59.0 / 59.0, 0 | 5.59 / 63.1 / 63.1, 0 | 8.50 / 22.8 / 22.8, 0 |
| 600 golden random | 0.69 / 1.84 / 6.11, 0 | 4.37 / 23.5 / 108, 0 | 4.44 / 41.4 / 475, 7 | 4.64 / 40.9 / 544, 8 | 4.54 / 154 / 50,528, 30 |
| 300 adversarial | 1.70 / 7.68 / 13.3, 0 | 20.8 / 87.5 / 172, 1 | 18.3 / 83.3 / 3,701, 3 | 19.1 / 84.6 / 3,760, 3 | 33.2 / 427 / 8,298, 30 |

## One ranked evaluate (what a spoken schedule costs; p50 / p95 / max ms, over 150 ms)

| set (n evaluates) | client_first | client_first, prop. | fee_first, prop. |
|---|---|---|---|
| 7 scenarios + demo × 100 bp (14,400) | 0.04 / 0.14 / 0.31, 0 | 0.04 / 0.13 / 0.40, 0 | 0.06 / 0.84 / 9.1, 0 |
| fixtures + golden × 100 bp (60,500) | 0.04 / 0.33 / 64.1, 0 | 0.04 / 0.33 / 66.6, 0 | 0.04 / 0.60 / 1,172, 139 |
| adversarial × 100 bp (30,000) | 0.15 / 0.81 / 314, 14 | 0.16 / 0.84 / 317, 14 | 0.25 / 1.78 / 279, 79 |

## Against the budget

- **Scan p50 ≤ 2× the 57b figure.** The adapter scan on the demo is 0.66 ms against 57b's
  1.90 ms (0.35×). Golden p50 0.69 ms against 3.08 ms.
- **Max ≤ 150 ms per scan on scenarios, demo and golden.** Met by the adapter scan (worst
  6.1 ms) in every mode, since it does not rank. One ranked evaluate in the default mode
  (client_first, front-loaded) is at most 64 ms on these sets.
- **Ranked 100-point scans (not on the agent path).** client_first: 7 of 600 golden scans
  are over 150 ms (max 475 ms). fee_first + proportional cannot prune on Φ (not
  monotone), so it scores the whole family: a golden scan reaches 50.5 s and one
  evaluate 1.17 s. That mode is opt-in. Recorded as an open issue with a fix (a
  two-sided bound: lower prefix sums for the window term, upper ones for the cap).
- **Adversarial** (not in the budget): one client_first case (`a57_00179`, 4 levels, caps
  on the floors) takes 3.7 s per ranked scan. It took 359 s before two fixes:
  memoising staircase sub-problems that come up empty, and cutting a sub-problem at once
  when caps pin the first payments to their floors and so use up levels.

## What made client_first affordable

- Stage 1 (`C*`) bisects on C instead of improving a cushion incumbent cent by cent;
  each test stops at the first vector under the caps.
- Stage 2 (`T*`) starts at the stage-1 vector's largest payment; each k costs one
  "can it beat this" test and one "can it tie" test, and only an improving k is searched.
- Stage 3 runs branch and bound on the monotone `key4` through a hook that sees lower
  bounds on every prefix sum (front-loaded only).
- Empty staircase sub-problems are memoised per caps; forced floor levels cut early.
