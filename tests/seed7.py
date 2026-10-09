"""Cheap, exact stand-in for slots of ``sim.scenarios.generate(100, 7)``.

``generate(100, 7)`` takes ~20 s because it samples all 100 scenarios. Slot
``i`` is just ``generate_one(PERSONAS[i % 3], <stratum of slot i>, sub_seed_i)``
re-ided (plus its Phase 46a haggle style, ``apply_haggle``), where
``sub_seed_i`` is the i-th draw of ``Random(7)``. Rebuilding one
slot costs tens of ms, so fast tests use these instead of the full set.
``tests/e2e/test_policy_invariants.py::test_seed7_slots_match_generate`` (slow)
checks that this stays identical to ``generate``.
"""

from __future__ import annotations

from dataclasses import replace
from functools import cache, lru_cache
from random import Random

from sim.personas import PERSONAS
from sim.scenarios import STRATA, Scenario, apply_haggle, balanced_quota, generate_one

N = 100
SEED = 7


@lru_cache(maxsize=1)
def _plan() -> tuple[tuple[str, int], ...]:
    """(stratum, sub_seed) per slot, in ``generate`` order."""
    rng = Random(SEED)
    quota = balanced_quota(N)
    return tuple(
        (stratum, rng.randint(0, 2**31 - 1)) for stratum in STRATA for _ in range(quota[stratum])
    )


@cache
def easy_slot(i: int) -> Scenario:
    """Slot ``i`` before its Phase 46a haggle style: the pre-46a easy rep, floor and ask."""
    stratum, sub_seed = _plan()[i]
    persona = PERSONAS[i % len(PERSONAS)]
    sc = generate_one(persona, stratum, seed=sub_seed)  # type: ignore[arg-type]
    sid = f"s{SEED:04d}_{i:03d}_{stratum}_{persona}"
    return replace(sc, id=sid, call=replace(sc.call, id=sid))


@cache
def slot(i: int) -> Scenario:
    """``generate(100, 7)[i]``, id and haggle style included."""
    return apply_haggle(easy_slot(i), seed=SEED, index=i)


@lru_cache(maxsize=1)
def tiered() -> tuple[Scenario, ...]:
    """Every scenario of ``generate(100, 7)`` with non-empty tiers (only deal slots get them)."""
    deal = [i for i, (stratum, _) in enumerate(_plan()) if stratum == "deal"]
    return tuple(slot(i) for i in deal if slot(i).true_rules.min_payment_tiers)
