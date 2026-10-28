"""Exact reference solver checks: oracle agreement, dominance, pruning, a known take-home miss.

- ``reference.solve`` equals ``oracle.solve`` (feasibility, Φ*, lump, increment) on
  seeded small cases (500 fast; the Phase 55 sets of 5,000 + 10,000 under ``slow``).
- The dominating family really dominates: on small rules every legal vector
  (brute-force enumeration, ``oracle.is_legal``) has a family vector of the same length
  with pointwise smaller or equal prefix sums, and every family vector is legal.
- Pruning never changes the answer (full-size adversarial cases, family scored whole).
- A shrunk Phase 57 witness (the take-home family called it infeasible): the reference
  schedule is legal, validator-clean and feasible on an independent replay, and the
  engine finds a schedule too.
The Phase 57 hunt is frozen in ``docs/eval/engine_gap_20261025_full/``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from itertools import accumulate
from random import Random

import pytest

from feasibility.models import CreditorRules
from feasibility.problem import build_problem
from feasibility.search import Search
from tests.engine import gen_cases, hunt, oracle, reference


def _agree(case: dict) -> tuple[bool, bool]:
    client, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    S = problem.offer_total(offer.settlement_bp)
    r = reference.solve(problem, S)
    o = oracle.solve(client, rules, problem.first_payment_date, S, problem.program_fee_cents)
    got = (r.feasible, r.phi, r.lump_cents, r.increment_cents)
    return got == (o.feasible, o.phi, o.lump_cents, o.increment_cents), o.feasible


def test_reference_matches_oracle_fast() -> None:
    res = [_agree(c) for c in gen_cases.random_cases(1, 500, small=True, prefix="g")]
    assert all(ok for ok, _ in res)
    assert sum(f for _, f in res) > 100


@pytest.mark.slow
@pytest.mark.parametrize("seed,n", [(1, 5000), (2, 10000)])
def test_reference_matches_oracle_phase55_sets(seed: int, n: int) -> None:
    bad = [c["id"] for c in gen_cases.random_cases(seed, n, small=True, prefix="g")
           if not _agree(c)[0]]
    assert bad == []


def _nondecreasing(S: int, k: int, lo: int = 0) -> Iterator[tuple[int, ...]]:
    if k == 1:
        if S >= lo:
            yield (S,)
        return
    for p in range(lo, S // k + 1):
        for rest in _nondecreasing(S - p, k - 1, p):
            yield (p, *rest)


def test_family_dominates_every_legal_vector() -> None:
    rng = Random(57)
    checked = 0
    for _ in range(120):
        k = rng.randint(1, 7)
        base = rng.randint(1, 6)
        tiers = sorted(
            (rng.randint(2, max(2, k)), base + rng.randint(1, 8))
            for _ in range(rng.choice([0, 1, 2]))
        )
        even, balloon = rng.choice([(False, False), (False, False), (False, True), (True, False)])
        rules = CreditorRules(k, k, base, rng.randint(0, k), tiers, even, balloon,
                              rng.randint(1, 4), 0, program_fee_bp=0)
        S = rng.randint(k * base, k * base + 45)
        fam = list(reference.Reference.structural(rules, S, k).family(k))
        assert all(oracle.is_legal(v, S, k, rules) for v in fam)
        fam_p = [list(accumulate(v)) for v in fam]
        for v in _nondecreasing(S, k):
            if not oracle.is_legal(v, S, k, rules):
                continue
            P = list(accumulate(v))
            assert any(all(a <= b for a, b in zip(q, P)) for q in fam_p), (rules, S, v)
            checked += 1
    assert checked > 800


def test_pruning_does_not_change_the_answer() -> None:
    keys = ("ref_feasible", "ref_phi", "ref_lump", "ref_increment")
    for case in gen_cases.adversarial_cases(5, 25):
        a = hunt.check_reference(case)
        b = hunt.check_reference(case, prune=False)
        assert [a.get(x) for x in keys] == [b.get(x) for x in keys], case["id"]
        assert a["bugs"] == [] and b["bugs"] == []


def test_known_take_home_false_infeasible() -> None:
    """Shrunk witness ``s57_01877`` from the Phase 57 report (3 segments, two tiers)."""
    rules = {
        "max_terms": 5, "max_payments": 5, "min_payment_cents": 6, "max_token_pays": 5,
        "min_payment_tiers": [[2, 10], [4, 90]], "even_pays": False,
        "is_ballooning_allowed": False, "max_segments": 3, "bank_fee_cents": 0,
    }
    vec = (7, 10, 10, 92, 92)
    S, k = sum(vec), len(vec)
    case = hunt.structural_witness_case("w", rules, vec, t1=date(2027, 7, 15), F=0)
    # The take-home family is the search's pinned stage: none of its vectors fits.
    _, _, _, problem = hunt._problem(case)
    search = Search(problem, S)
    pinned = [v for j in range(1, k + 1) for v in search.pinned(j, lambda _: False)]
    assert pinned and all(search.key4(list(accumulate(v)), len(v), 0) is None for v in pinned)
    rec = hunt.check_reference(case)
    assert rec["ref_feasible"] and rec["bugs"] == []
    assert rec["ref_vec"] == list(vec)
    eng = hunt.compare_engine(case)
    assert eng["feasible"] and eng["kinds"] == [] and eng["bugs"] == []


def test_adversarial_generator_is_seeded_and_planted() -> None:
    a = gen_cases.adversarial_cases(3, 20)
    assert a == gen_cases.adversarial_cases(3, 20)
    for case in a:
        assert case["creditor_balance_cents"] == 2 * sum(case["planted"])
        assert hunt.check_reference(case)["bugs"] == []
