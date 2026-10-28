"""Objectives and fee policy (``feasibility/objectives.py``, Phase 59).

- Engine vs ``tests/engine/oracle.solve_ranked`` (every legal vector) on small cases:
  ``client_first`` + ``front_loaded`` agrees on (cushion, largest payment, Φ, k);
  ``fee_first`` agrees on Φ* under ``front_loaded``. Under ``proportional`` it agrees on
  feasibility, cushion and the largest payment. Φ there is exact over the family only,
  never above the oracle's, and the gap is counted (docs/ENGINE.md).
- The capped family ``ClientFirst.family(k, T)`` dominates every legal vector with all
  payments <= T (brute force on small rules), and every member is legal.
- Full size: ``C*`` equals the maximum over the reference's dominating family; every
  client-first schedule passes the validator; ``is_feasible`` equals ``evaluate``.
- ``evaluate_offer`` stays fee-first (the golden corpus is checked by ``test_golden.py``).
"""

from __future__ import annotations

import os
from itertools import accumulate
from random import Random

import pytest

from app.adapter.validator import RULE_FEE_PROPORTIONAL, validate
from feasibility.engine import evaluate, evaluate_offer, is_feasible
from feasibility.models import CreditorRules, Offer
from feasibility.objectives import (
    CLIENT_FIRST,
    FEE_FIRST,
    PROPORTIONAL,
    ClientFirst,
    check_fee_policy,
    check_objective,
    cushion_of,
)
from feasibility.problem import Problem, build_problem
from feasibility.search import INF, Search
from tests.engine import gen_cases, oracle, reference
from tests.engine.golden.build_golden import SCENARIOS, scenario_case


def _problem(case: dict, fee_policy: str = "front_loaded") -> tuple[Problem, int]:
    client, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
        fee_policy=fee_policy,
    )
    return problem, offer.settlement_bp


def _payments(res) -> tuple[int, ...]:
    return tuple(r.creditor_payment_cents for r in res.schedule if r.creditor_payment_cents > 0)


# --- engine vs oracle, small cases ---------------------------------------------------


# How much of the compared key is claimed exact (proportional Φ is not monotone).
EXACT_PREFIX = {
    (CLIENT_FIRST, "front_loaded"): 4,  # cushion, largest payment, Φ, k
    (FEE_FIRST, "front_loaded"): 1,  # Φ*
    (CLIENT_FIRST, PROPORTIONAL): 2,  # cushion, largest payment
    (FEE_FIRST, PROPORTIONAL): 0,  # feasibility only
}


def _oracle_sweep(seed: int, n: int, objective: str, policy: str) -> dict[str, int]:
    """Counts of agreements and gaps; ``bad`` must stay 0, ``phi_gap`` only under proportional."""
    out = {"feasible": 0, "bad": 0, "phi_gap": 0}
    for case in gen_cases.random_cases(seed, n, small=True, prefix="g"):
        problem, bp = _problem(case, policy)
        S, F = problem.offer_total(bp), problem.program_fee_cents
        try:
            o = oracle.solve_ranked(
                problem.client, problem.rules, problem.first_payment_date, S, F, objective, policy
            )
        except oracle.OracleTooBig:
            continue
        res = evaluate(problem, bp, objective)
        if res.feasible != o.feasible:
            out["bad"] += 1
            continue
        if not o.feasible:
            continue
        out["feasible"] += 1
        vec = _payments(res)
        inst = oracle.Instance(problem.client, problem.rules, problem.first_payment_date, S, F)
        bal = inst.replay(vec)
        phi = inst.phi(bal, vec if policy == PROPORTIONAL else None)
        assert phi is not None
        cushion = min(inst.windows(bal)) - F
        if objective == FEE_FIRST:
            key: tuple = (phi,)
            want: tuple = o.key[:1]
        else:
            key = (cushion, -vec[-1], phi, -len(vec))
            want = o.key
        exact = EXACT_PREFIX[(objective, policy)]  # docs/ENGINE.md exactness table
        if key[:exact] != want[:exact]:
            out["bad"] += 1
        elif key != want:
            assert key < want, (case["id"], key, want)  # never claims better than all-legal
            out["phi_gap"] += 1
    return out


@pytest.mark.parametrize(
    ("objective", "policy"),
    [
        (CLIENT_FIRST, "front_loaded"),
        (FEE_FIRST, "front_loaded"),
        (CLIENT_FIRST, PROPORTIONAL),
        (FEE_FIRST, PROPORTIONAL),
    ],
)
def test_engine_matches_ranked_oracle_small_fast(objective: str, policy: str) -> None:
    got = _oracle_sweep(3, 400, objective, policy)
    assert got["bad"] == 0 and got["feasible"] > 100
    if policy == "front_loaded":
        assert got["phi_gap"] == 0


@pytest.mark.slow
@pytest.mark.parametrize(
    ("objective", "policy"),
    [
        (CLIENT_FIRST, "front_loaded"),
        (FEE_FIRST, "front_loaded"),
        (CLIENT_FIRST, PROPORTIONAL),
        (FEE_FIRST, PROPORTIONAL),
    ],
)
def test_engine_matches_ranked_oracle_small_large_sets(objective: str, policy: str) -> None:
    """Seed 2 × 1,500 per mode by default (about 30 s each).

    ``ENGINE_ORACLE_FULL=1`` runs the Phase 59 evidence sets instead: seed 1 × 5,000 and
    seed 2 × 10,000 per mode (about 18 minutes for the four modes; PROGRESS Phase 59).
    """
    full = os.environ.get("ENGINE_ORACLE_FULL") == "1"
    sets = ((1, 5000), (2, 10000)) if full else ((2, 1500),)
    total = {"feasible": 0, "bad": 0, "phi_gap": 0}
    for seed, n in sets:
        for key, v in _oracle_sweep(seed, n, objective, policy).items():
            total[key] += v
    print(objective, policy, total)  # pytest -s shows the counts
    assert total["bad"] == 0 and total["feasible"] > (4000 if full else 400)
    if policy == "front_loaded":
        assert total["phi_gap"] == 0
    else:  # the measured gap (PROGRESS Phase 59): a few percent of feasible cases
        assert total["phi_gap"] < total["feasible"] * 0.05


# --- the capped dominating family, brute force ------------------------------------


def _legal_vectors(S: int, k: int, k_cap: int, rules: CreditorRules) -> list[tuple[int, ...]]:
    out: list[tuple[int, ...]] = []

    def rec(prefix: list[int], left: int, lo: int) -> None:
        r = k - len(prefix)
        if r == 0:
            if left == 0 and oracle.is_legal(tuple(prefix), S, k_cap, rules):
                out.append(tuple(prefix))
            return
        for p in range(lo, left // r + 1 if r > 1 else left + 1):
            if r == 1 and p != left:
                continue
            rec([*prefix, p], left - p, p)

    rec([], S, 1)
    return out


def _small_rules(rng: Random) -> CreditorRules:
    even = rng.random() < 0.15
    balloon = not even and rng.random() < 0.35
    k = rng.randint(2, 5)
    base = rng.randint(1, 6)
    tiers = sorted(
        [[rng.randint(2, k), base + rng.randint(1, 8)] for _ in range(rng.randint(0, 2))]
    )
    return CreditorRules(
        max_terms=k, max_payments=k, min_payment_cents=base,
        max_token_pays=rng.randint(0, k), min_payment_tiers=[tuple(t) for t in tiers],
        even_pays=even, is_ballooning_allowed=balloon, max_segments=rng.randint(1, 4),
        bank_fee_cents=0, program_fee_bp=0,
    )


def test_capped_family_dominates_every_capped_legal_vector() -> None:
    rng = Random(59)
    long = (c for c in gen_cases.random_cases(55, 100) if len(c["client"]["ledger"]) >= 8)
    client, offer, _ = gen_cases.case_to_objects(next(long))  # horizon >= 5 cadence dates
    checked = 0
    for _ in range(250):
        rules = _small_rules(rng)
        problem = build_problem(
            client, rules, offer.first_payment_date,
            creditor_balance_cents=offer.creditor_balance_cents,
            original_balance_cents=offer.original_balance_cents,
        )
        if problem.k_max < 1:
            continue
        S = rng.randint(4, 60)
        cf = ClientFirst(Search(problem, S), 0)
        for k in range(1, problem.k_max + 1):
            legal = _legal_vectors(S, k, problem.k_max, rules)
            for T in sorted({v[-1] for v in legal}):
                fam = list(cf.family(k, T))
                for v in fam:
                    assert oracle.is_legal(v, S, problem.k_max, rules) and v[-1] <= T, v
                fam_p = [list(accumulate(v)) for v in fam]
                for q in legal:
                    if q[-1] > T:
                        continue
                    Q = list(accumulate(q))
                    assert any(all(a <= b for a, b in zip(p, Q)) for p in fam_p), (rules, S, T, q)
                    checked += 1
    assert checked > 2000


# --- full size --------------------------------------------------------------------


def _full_cases() -> list[dict]:
    out = [{**scenario_case(n), "settlement_bp": bp} for n in SCENARIOS for bp in (3000, 5000)]
    out += gen_cases.random_cases(55, 150)
    out += gen_cases.adversarial_cases(57, 60)
    return out


def test_best_cushion_equals_reference_family_maximum() -> None:
    """C* = max cushion over the Phase 57 reference family (dominating, independent)."""
    n = 0
    for case in _full_cases():
        problem, bp = _problem(case)
        S = problem.offer_total(bp)
        search = Search(problem, S)
        got = ClientFirst(search, 0).best_cushion()
        ref = reference.Reference(problem, S, prune=False)
        best = None
        for k in range(1, problem.k_max + 1):
            if search._phi_tail(k, 0) is None:
                continue
            for vec in ref.family(k):
                P = list(accumulate(vec))
                if search.key4(P, k, 0) is None:
                    continue
                c = cushion_of(search, P, k)
                best = c if best is None or c > best else best
        assert got == best, case["id"]
        n += best is not None
    assert n > 50


@pytest.mark.parametrize("policy", ["front_loaded", PROPORTIONAL])
def test_client_first_schedules_valid_and_keys_consistent(policy: str) -> None:
    for case in _full_cases():
        problem, bp = _problem(case, policy)
        res = evaluate(problem, bp, CLIENT_FIRST)
        fee_first = evaluate(problem, bp, FEE_FIRST)
        assert res.feasible == fee_first.feasible == is_feasible(problem, bp)
        if not res.feasible:
            assert res.to_dict()["additional_funds"] == fee_first.to_dict()["additional_funds"]
            continue
        S, F = problem.offer_total(bp), problem.program_fee_cents
        v = validate(res.schedule, problem.client, S, F, problem.rules,
                     problem.first_payment_date, fee_policy=policy)
        assert v == [], (case["id"], v)
        search = Search(problem, S)
        vec, ff = _payments(res), _payments(fee_first)
        c_cf = cushion_of(search, list(accumulate(vec)), len(vec))
        c_ff = cushion_of(search, list(accumulate(ff)), len(ff))
        assert (c_cf, -vec[-1]) >= (c_ff, -ff[-1]), case["id"]


@pytest.mark.parametrize("policy", ["front_loaded", PROPORTIONAL])
def test_is_feasible_equals_evaluate_on_scans(policy: str) -> None:
    for name in SCENARIOS:
        problem, _ = _problem({**scenario_case(name), "settlement_bp": 100}, policy)
        for bp in range(100, 10001, 100):
            assert is_feasible(problem, bp) == evaluate(problem, bp).feasible, (name, bp)
    for case in gen_cases.random_cases(55, 150) + gen_cases.adversarial_cases(57, 40):
        problem, _ = _problem(case, policy)
        for bp in range(500, 10001, 500):
            assert is_feasible(problem, bp) == evaluate(problem, bp).feasible, (case["id"], bp)


def test_proportional_schedules_respect_the_cap_and_front_loaded_may_not() -> None:
    flagged = 0
    for case in gen_cases.random_cases(55, 200):
        prop, bp = _problem(case, PROPORTIONAL)
        res = evaluate(prop, bp, FEE_FIRST)
        if not res.feasible:
            continue
        S, F = prop.offer_total(bp), prop.program_fee_cents
        args = (prop.client, S, F, prop.rules, prop.first_payment_date)
        assert validate(res.schedule, *args, fee_policy=PROPORTIONAL) == []
        front = evaluate(_problem(case)[0], bp, FEE_FIRST)
        assert validate(front.schedule, *args) == []  # rule off by default
        flagged += any(
            x.rule == RULE_FEE_PROPORTIONAL
            for x in validate(front.schedule, *args, fee_policy=PROPORTIONAL)
        )
    assert flagged > 10


def test_evaluate_offer_stays_fee_first_front_loaded() -> None:
    case = {**scenario_case("easy_deal"), "settlement_bp": 4000}
    client, offer, rules = gen_cases.case_to_objects(case)
    problem, bp = _problem(case)
    assert evaluate_offer(client, offer, rules).to_dict() == evaluate(problem, bp).to_dict()
    assert isinstance(offer, Offer)


def test_easy_deal_client_first_uses_the_allowed_count() -> None:
    """The Phase 25 open issue: 2 payments offered though more are allowed.

    At 40% ($500) the $100 minimum caps an even plan at 5 payments (8 would be $62.50).
    """
    problem, _ = _problem({**scenario_case("easy_deal"), "settlement_bp": 4000})
    assert _payments(evaluate(problem, 4000, FEE_FIRST)) == (25000, 25000)
    assert _payments(evaluate(problem, 4000, CLIENT_FIRST)) == (10000,) * 5


def test_unknown_mode_names_raise() -> None:
    assert check_objective(CLIENT_FIRST) == CLIENT_FIRST
    assert check_fee_policy(PROPORTIONAL) == PROPORTIONAL
    with pytest.raises(ValueError):
        check_objective("cheapest")
    with pytest.raises(ValueError):
        check_fee_policy("back_loaded")
    case = {**scenario_case("easy_deal"), "settlement_bp": 4000}
    with pytest.raises(ValueError):
        _problem(case, "back_loaded")
    problem, _ = _problem(case)
    with pytest.raises(ValueError):
        evaluate(problem, 4000, "cheapest")
    assert INF > 10**15
