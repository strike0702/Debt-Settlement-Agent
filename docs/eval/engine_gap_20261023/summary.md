# Engine gap report: legacy generator vs brute-force oracle (2026-10-23)

Command: `uv run python scripts/engine_gap_report.py --cases 5000 --seed 1` (48.1 s). Cases: small seeded random (`tests/engine/gen_cases.py`, small mode: S ≤ 400 cents, k_max ≤ 6, M ≤ 8, all three structures, tiers, token limits, committed debits, mid-month and EOM starts).

Legacy = `feasibility/legacy.py` (the take-home candidate family, frozen at Phase 55) scored by the engine. Oracle = `tests/engine/oracle.py` (every vector legal under binding rules 1–9, suffix-min fee, Part 2 by binary search). Evidence only; nothing is fixed here.

| | count |
|---|---|
| cases | 5000 |
| oracle skipped (over enumeration budget) | 0 |
| legacy feasible / oracle feasible | 1572 / 1572 |
| both feasible (Φ* compared) | 1572 |
| both infeasible (rescue compared) | 3428 |
| … of which k_max = 0 (no cadence date before the horizon or a zero payment cap) | 351 |
| … of which oracle lump fixable / increment fixable | 2810 / 2810 |
| False infeasible (legacy walks away from a legal deal) | 0 of 5000 |
| Worse Φ* (legacy collects the fee later than possible) | 0 of 1572 |
| Rescue over-estimate: lump sum | 0 of 3428 |
| Rescue over-estimate: monthly increment | 0 of 3428 |

## Consistency checks (must all be 0)

| check | count |
|---|---|
| legacy feasible oracle not | 0 |
| illegal legacy vector | 0 |
| legacy phi mismatch | 0 |
| legacy phi above oracle | 0 |
| rescue under | 0 |
| validator on oracle | 0 |

## Prefix-cap model sweep (generator only, no ledger)

Random prefix caps for a fixed k: a vector fits iff `P_j <= cap_j` for j < k, Φ is the suffix-min of the slacks (prefix-cap identity, `feasibility/problem.py`). Every non-decreasing vector legal under rules 1–9 is enumerated and compared with the legacy family for the same k.

| draws | k ≤ | S ≤ | feasible | legacy miss | legacy worse Φ |
|---|---|---|---|---|---|
| 4000 | 6 | 40 | 1051 | 0 | 0 |
| 4000 | 8 | 60 | 1085 | 0 | 0 |

## False infeasible (legacy walks away from a legal deal)

None found.

## Worse Φ* (legacy collects the fee later than possible)

None found.

## Rescue over-estimate: lump sum

None found.

## Rescue over-estimate: monthly increment

None found.
