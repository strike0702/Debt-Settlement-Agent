# Engine gap report: reading the result (Phase 55)

`summary.md` is the generated report (`--cases 5000 --seed 1`). This note says what it
does and does not show.

## Result

On small instances the frozen take-home generator (`feasibility/legacy.py`) matched the
brute-force oracle on every measure:

- 5,000 cases (seed 1): 0 false infeasible, 0 worse Φ\*, 0 lump or increment
  over-estimates; all consistency checks 0 (legacy vectors legal, legacy fee timing equal
  to the oracle's suffix-min Φ for the same vector, no rescue under-estimate, every oracle
  schedule passes `app/adapter/validator.py`).
- 80,000 more cases (seeds 2–9, 10,000 each, `--cap-draws 0`): all buckets 0.
- Prefix-cap model (no ledger, random caps, every legal vector for a fixed k): 0 misses and
  0 worse Φ at k ≤ 6, S ≤ 40 and k ≤ 8, S ≤ 60 (table in `summary.md`).

The oracle itself is checked against an unpruned enumeration of every non-decreasing
vector on tiny cases (`tests/engine/test_oracle.py`), including both Part 2 minima.

## What it does not show

- **Large instances.** The oracle stops at S ≤ ~400 cents, k_max ≤ 6, M ≤ 8, and the cap
  model at k ≤ 8. The plan's weakness 1 (the divisibility pin on early staircase levels)
  needs several levels, long payment runs and tight caps at once. Real cases have k up
  to 24 and S in the thousands of dollars, and nothing here rules out a miss there.
- **Why it holds at this size.** For each composition, the legacy family keeps the
  floors-pinned early levels plus the smallest workable last two levels. It also keeps
  the quasi-even vector and the floors-pinned balloon. In every small cap shape drawn,
  one of these had pointwise-minimal prefix sums among the legal vectors. That is
  observed, not proved.

## Consequence for Phase 57

At small scale, Phase 57's exact search cannot be justified by a measured legacy miss.
Its case rests on (a) a completeness argument for large k that the brute force cannot
reach, and (b) Phase 59/60, which need a search over the full legal set, not a fixed
family. Phase 57 should compare exact search against legacy on the 600 full-size golden
random cases and on a larger full-size seeded set, where exact search is the reference.
It should report any difference with a validator-checked witness.

Reproduce:

    uv run python scripts/engine_gap_report.py --cases 5000 --seed 1
    for s in 2 3 4 5 6 7 8 9; do
      uv run python scripts/engine_gap_report.py --cases 10000 --seed $s --cap-draws 0 --out /tmp/gap_$s
    done
