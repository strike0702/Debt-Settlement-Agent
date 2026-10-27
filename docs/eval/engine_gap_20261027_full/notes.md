# Engine gap report after Phase 57b: reading the result

`summary.md` is the generated report (same command and seed as Phase 57):

    uv run python scripts/engine_gap_report.py --mode full --seed 57 --structural 5000

Since Phase 57b the report has a second table. It compares the **engine itself**
(`feasibility.engine.evaluate`, now the exact search in `feasibility/search.py`) with
the exact reference on the same cases. Legacy (`feasibility/legacy.py`, the frozen
take-home family) is still measured, so the comparison stays the same until Phase 61.

## Result

| set | cases | legacy misses (false inf. / worse Φ\* / lump over / incr. over) | engine misses |
|---|---|---|---|
| golden | 1,305 | 0 / 0 / 0 / 0 | **0** |
| seeded random | 20,000 | 0 / 0 / 0 / 0 | **0** |
| adversarial | 20,000 | 2 / 2 / 13 / 8 | **0** |
| structural witnesses | 112 | 112 false infeasible | **0** |

- Engine bugs (validator violations on its schedules, rescue below the reference,
  feasible where the reference is not): 0 on every set.
- Legacy numbers are identical to Phase 57 (`engine_gap_20261025_full`). Same seed, same
  family, so this also checks that the report did not drift.
- Reference vs oracle (15,000 small cases) and the pruning self-check (900 cases) are
  still at 0 disagreements.

## `witnesses.jsonl`

Every case where legacy misses (18 adversarial cases carrying the 25 miss counts, plus
the 112 verified structural witnesses) is written here with its kinds. The same file is
frozen as `tests/engine/golden/witnesses_57.jsonl`. `tests/engine/test_search.py`
checks that legacy still misses each one the recorded way and that the engine matches
the reference on all of them. It also checks that topping up the client by exactly
L\* (or every draft by X\*) makes the offer feasible, and one cent less does not.

## What is still not proved

Legacy (the take-home family) being complete for rules **without tiers** remains a
conjecture: 0 non-dominated vectors among 28 M family vectors in 12,483 tier-free draws,
but no proof (see `engine_gap_20261025_full/notes.md`). The engine no longer depends on
it, because its box stage covers every staircase with ≥ 3 segments whether or not there
are tiers.
