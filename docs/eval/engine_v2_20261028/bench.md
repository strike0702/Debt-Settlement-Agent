# Engine v2, Phase 61: benchmark of the finished engine (2026-10-28)

Same machine as the Phase 57b and 59 runs ([`../engine_v2_20261027/bench.md`](../engine_v2_20261027/bench.md)):
Apple M5, one process. Branch `phase-61` (main `25fd375` plus the Phase 61 removals,
which do not touch the search). Another worktree was running tests at the same time, so
small figures can be a few percent noisy.

    uv run python scripts/engine_bench.py --repeat 15 --adversarial 300 --scan feasible
    uv run python scripts/engine_bench.py --repeat 15 --adversarial 300
    uv run python scripts/engine_bench.py --repeat 15 --adversarial 300 --objective client_first

Raw output: [`bench_feasible.json`](bench_feasible.json), [`bench_fee_first.json`](bench_fee_first.json),
[`bench_client_first.json`](bench_client_first.json). A scan is `build_problem` once, then 100
prices (bp 100..10000). "Adapter scan" is what the agent runs on a cache miss
(`engine.is_feasible` per point, no ranking). "Ranked" runs `engine.evaluate` at every
point in that objective; the agent ranks only once per spoken plan.

## Fixed targets (median of 15 cold 100-point scans, ms)

| target | adapter scan | fee_first ranked | client_first ranked |
|---|---|---|---|
| demo, even rules (`fixtures/demo`) | 0.46 | 1.94 | 5.95 |
| demo, 4-level staircase + tier | 0.61 | 3.70 | 13.95 |
| balloon_structure | 0.64 | 3.09 | 4.40 |
| counter_ladder | 0.57 | 2.11 | 6.34 |
| easy_deal | 0.60 | 2.06 | 6.37 |
| haggling_rep | 0.69 | 2.37 | 6.26 |
| late_start_date | 0.12 | 0.36 | 0.37 |
| no_space | 0.49 | 3.08 | 3.13 |
| rescue_escalate | 0.30 | 3.10 | 2.98 |

## Case sets: 100-point scans (p50 / p95 / max ms, scans over 150 ms)

| set | adapter scan | fee_first ranked | client_first ranked |
|---|---|---|---|
| 5 engine fixtures | 0.47 / 0.71 / 0.71, 0 | 2.76 / 5.87 / 5.87, 0 | 5.69 / 59.6 / 59.6, 0 |
| 600 golden random | 0.51 / 1.45 / 4.83, 0 | 3.12 / 17.3 / 78.3, 0 | 4.49 / 41.6 / 479, 7 |
| 300 adversarial | 1.25 / 5.22 / 9.63, 0 | 15.2 / 64.4 / 126, 0 | 18.9 / 85.6 / 3,718, 4 |

## One ranked evaluate (p50 / p95 / max ms, over 150 ms)

| set (n evaluates) | fee_first | client_first |
|---|---|---|
| 7 scenarios + demo × 100 bp (14,400) | 0.02 / 0.04 / 0.18, 0 | 0.04 / 0.14 / 0.84, 0 |
| fixtures + golden × 100 bp (60,500) | 0.03 / 0.16 / 3.35, 0 | 0.04 / 0.34 / 66.6, 0 |
| adversarial × 100 bp (30,000) | 0.13 / 0.74 / 3.28, 0 | 0.16 / 0.82 / 307, 14 |

## Against the budget and earlier runs

- **Budget** (max 150 ms per scan on the scenarios, the demo and the golden cases): met
  by the adapter scan in every set, worst 4.83 ms. The default ranked mode (fee_first)
  also meets it as a full 100-point scan (worst 78.3 ms).
- **Against the take-home engine** (Phase 57b "before", same machine): demo ranked scan
  3.61 → 1.94 ms, golden max 10,753 → 78.3 ms, adversarial max 9,158 → 126 ms. The
  agent's scan is 0.46 ms on the demo.
- **Against Phase 59** (same commands, 2026-10-27): within noise (adapter scan on the demo
  0.66 → 0.46 ms; client_first golden max 475 → 479 ms; the adversarial client-first
  case `a57_00179` still about 3.7 s). Phase 60 and 61 did not change the search.
- **Not run again:** the proportional fee policy (opt-in; Phase 59 figures stand: a
  fee_first + proportional golden scan up to 50.5 s, one evaluate up to 1.17 s).
