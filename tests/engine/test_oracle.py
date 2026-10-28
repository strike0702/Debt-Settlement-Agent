"""Brute-force oracle checks: its pruning is sound, and the engine agrees with it on legality.

- On tiny cases the pruned ``oracle.solve`` equals an unpruned enumeration of every
  non-decreasing vector (feasibility, Φ*, Part 2 lump and increment).
- ``oracle.is_legal`` agrees with the independent validator's static rules on random
  vectors (legal and illegal).
- On seeded small cases every engine-feasible answer is oracle-feasible, its vector is
  legal, its fee timing equals the oracle's suffix-min Φ for that vector and is never
  above Φ*; every oracle schedule passes ``app/adapter/validator.py``.
Engine-vs-oracle optimality (Φ*, L*, X*) is in ``test_search.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from random import Random

import pytest

from app.adapter.validator import (
    RULE_EVEN_VECTOR,
    RULE_EXACT_SUM,
    RULE_FLOORS,
    RULE_NON_DECREASING,
    RULE_SEGMENTS,
    RULE_TOKEN_COUNT,
    validate,
)
from feasibility.engine import ScheduleRow, evaluate
from feasibility.problem import build_problem
from tests.engine import gen_cases, oracle

STATIC_RULES = {
    RULE_EXACT_SUM,
    RULE_NON_DECREASING,
    RULE_FLOORS,
    RULE_TOKEN_COUNT,
    RULE_EVEN_VECTOR,
    RULE_SEGMENTS,
}


def _setup(case: dict):
    client, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    return client, rules, problem, problem.offer_total(offer.settlement_bp)


def _nondecreasing(S: int, k: int, lo: int = 0) -> Iterator[tuple[int, ...]]:
    if k == 1:
        if S >= lo:
            yield (S,)
        return
    for p in range(lo, S // k + 1):
        for rest in _nondecreasing(S - p, k - 1, p):
            yield (p, *rest)


def _brute_phi(inst: oracle.Instance, L: int = 0, X: int = 0) -> tuple[int, ...] | None:
    best = None
    for k in range(1, inst.k_cap + 1):
        for v in _nondecreasing(inst.S, k):
            if oracle.is_legal(v, inst.S, inst.k_cap, inst.rules):
                phi = inst.phi(inst.replay(v, L, X))
                if phi is not None and (best is None or phi > best):
                    best = phi
    return best


def _brute_min(ok, hi: int) -> int | None:
    if not ok(hi):
        return None
    return next(a for a in range(hi + 1) if ok(a)) if hi < 64 else _bisect(ok, hi)


def _bisect(ok, hi: int) -> int:
    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def _tiny_cases(n: int, seed: int) -> list[dict]:
    rng = Random(seed)
    out: list[dict] = []
    i = 0
    while len(out) < n:
        case = gen_cases.random_case(rng, f"t{seed}_{i}", small=True)
        i += 1
        _, _, problem, S = _setup(case)
        if S <= 70:
            out.append(case)
    return out


@pytest.mark.parametrize("case", _tiny_cases(120, 11), ids=lambda c: c["id"])
def test_pruned_oracle_equals_unpruned_enumeration(case: dict) -> None:
    client, rules, problem, S = _setup(case)
    F = problem.program_fee_cents
    got = oracle.solve(client, rules, problem.first_payment_date, S, F)
    inst = oracle.Instance(client, rules, problem.first_payment_date, S, F)
    want = _brute_phi(inst)
    assert got.phi == want
    assert got.feasible == (want is not None)
    if not got.feasible:
        hi = S + F + rules.bank_fee_cents * inst.k_cap + sum(inst.debit.values())
        hi += max(0, -client.current_balance_cents) + 1
        assert got.lump_cents == _brute_min(lambda a: _brute_phi(inst, L=a) is not None, hi)
        assert got.increment_cents == _brute_min(
            lambda a: _brute_phi(inst, X=a) is not None, hi
        )


def test_is_legal_matches_validator_static_rules() -> None:
    rng = Random(5)
    cases = gen_cases.random_cases(21, 200, small=True, prefix="v")
    checked = 0
    for case in cases:
        client, rules, problem, S = _setup(case)
        if problem.k_max == 0:
            continue
        for _ in range(15):
            k = rng.randint(1, problem.k_max)
            if rng.random() < 0.5:
                cuts = sorted(rng.randint(0, S) for _ in range(k - 1))
                vec = tuple(sorted(b - a for a, b in zip([0, *cuts], [*cuts, S])))
            else:
                vec = tuple(rng.randint(0, S) for _ in range(k))
            rows = [
                ScheduleRow(problem.cadence[i], p, 0, rules.bank_fee_cents, 0)
                for i, p in enumerate(vec)
            ]
            violations = validate(rows, client, S, 0, rules, problem.first_payment_date)
            static_ok = not any(v.rule in STATIC_RULES for v in violations)
            # The validator skips zero-payment rows; only compare vectors without zeros.
            if 0 in vec:
                continue
            assert oracle.is_legal(vec, S, problem.k_max, rules) == static_ok, (case["id"], vec)
            checked += 1
    assert checked > 1000


@pytest.mark.parametrize("seed", [101, 102, 103])
def test_engine_agrees_with_oracle_on_legality(seed: int) -> None:
    engine_feasible = 0
    for case in gen_cases.random_cases(seed, 150, small=True, prefix="o"):
        client, rules, problem, S = _setup(case)
        F = problem.program_fee_cents
        orc = oracle.solve(client, rules, problem.first_payment_date, S, F)
        if orc.feasible:
            assert orc.rows is not None
            assert validate(orc.rows, client, S, F, rules, problem.first_payment_date) == []
        res = evaluate(problem, case["settlement_bp"])
        if not res.feasible:
            continue
        engine_feasible += 1
        assert orc.feasible, case["id"]
        assert res.schedule is not None and orc.phi is not None
        vec = tuple(r.creditor_payment_cents for r in res.schedule if r.creditor_payment_cents)
        assert oracle.is_legal(vec, S, problem.k_max, rules), case["id"]
        fees = {r.date: r.program_fee_cents for r in res.schedule}
        run, phi = 0, []
        for t in problem.cadence:
            run += fees.get(t, 0)
            phi.append(run)
        assert oracle.phi_of(client, rules, problem.first_payment_date, S, F, vec) == tuple(phi)
        assert tuple(phi) <= orc.phi
    assert engine_feasible > 20


def test_validator_accepts_every_feasible_oracle_vector() -> None:
    """Not just Φ*-optimal ones: every feasible legal vector's schedule validates."""
    checked = 0
    for case in _tiny_cases(200, 12):
        client, rules, problem, S = _setup(case)
        F = problem.program_fee_cents
        inst = oracle.Instance(client, rules, problem.first_payment_date, S, F)
        for vec in inst.vectors():
            phi = inst.phi(inst.replay(vec))
            if phi is None:
                continue
            rows = inst.rows(vec, phi)
            assert validate(rows, client, S, F, rules, problem.first_payment_date) == []
            checked += 1
    assert checked > 50


def test_oracle_rejects_out_of_budget_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(
        c
        for c in gen_cases.random_cases(7, 50, small=True)
        if not c["rules"]["even_pays"] and _setup(c)[2].k_max > 0
    )
    client, rules, problem, S = _setup(case)
    monkeypatch.setattr(oracle, "NODE_BUDGET", 0)
    inst = oracle.Instance(client, rules, problem.first_payment_date, S, 0)
    with pytest.raises(oracle.OracleTooBig):
        list(inst.vectors(prune=False))
