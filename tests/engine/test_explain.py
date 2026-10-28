"""Why-infeasible (``feasibility/explain.py``) against the engine and the brute-force oracle.

- One test per reason code (``horizon``, ``floors``, ``segments``, ``cash_short``,
  ``fee_short``), each on the first seeded case that has it, checked independently.
- Exactness on small seeded cases: adding the shortfall as a credit on the binding date
  makes the oracle feasible with ``closest_k`` payments, and one cent less, even at the
  start of the ledger, leaves every payment count infeasible.
- ``explain`` is ``None`` exactly when the offer is feasible, and it does not depend on
  the objective.
"""

from __future__ import annotations

from dataclasses import replace
from functools import cache

import pytest

from feasibility.engine import is_feasible
from feasibility.explain import REASONS, Infeasibility, explain
from feasibility.models import Client, LedgerEntry
from feasibility.problem import Problem, build_problem
from tests.engine import gen_cases, oracle

GRID = range(100, 10001, 300)


def _problem(case: dict) -> Problem:
    client, offer, rules = gen_cases.case_to_objects(case)
    return build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )


@cache
def _small_points() -> list[tuple[Problem, int, Infeasibility | None]]:
    out = []
    for case in gen_cases.random_cases(1, 400, small=True):
        p = _problem(case)
        out.extend((p, bp, explain(p, bp)) for bp in GRID)
    return out


def _first(reason: str) -> tuple[Problem, int, Infeasibility]:
    for p, bp, e in _small_points():
        if e is not None and e.reason == reason:
            return p, bp, e
    raise AssertionError(f"no seeded case with reason {reason}")


def _instance(p: Problem, bp: int, client: Client | None = None, rules=None) -> oracle.Instance:
    return oracle.Instance(
        client or p.client, rules or p.rules, p.first_payment_date, p.offer_total(bp),
        p.program_fee_cents,
    )


def _with_credit(client: Client, amount: int, on) -> Client:
    return replace(client, ledger=[*client.ledger, LedgerEntry(on, amount, "credit")])


def _feasible_at_k(inst: oracle.Instance, k: int) -> bool:
    return any(
        len(v) == k and inst.phi(inst.replay(v)) is not None for v in inst.vectors()
    )


def _assert_lump_exact(p: Problem, bp: int, e: Infeasibility) -> None:
    assert e.shortfall_cents is not None and e.shortfall_cents > 0
    assert e.binding_date is not None and e.closest_k is not None
    topped = _with_credit(p.client, e.shortfall_cents, e.binding_date)
    assert _feasible_at_k(_instance(p, bp, topped), e.closest_k)
    assert not _instance(p, bp).feasible_with(L=e.shortfall_cents - 1)


def test_reason_codes_are_the_documented_five() -> None:
    assert set(REASONS) == {"horizon", "floors", "segments", "cash_short", "fee_short"}


def test_horizon() -> None:
    p, bp, e = _first("horizon")
    assert p.M == 0 or p.k_max == 0
    assert _instance(p, bp).M == 0 or _instance(p, bp).k_cap == 0
    assert e.shortfall_cents is None and e.closest_k is None
    assert e.binding_date == p.first_payment_date


def test_floors() -> None:
    p, bp, e = _first("floors")
    S = p.offer_total(bp)
    assert e.closest_k == 1 and e.shortfall_cents == p.floors[1] - S > 0
    assert e.binding_date == p.cadence[0]
    assert next(_instance(p, bp).vectors(prune=False), None) is None  # no legal vector
    assert oracle.is_legal((S + e.shortfall_cents,), S + e.shortfall_cents, p.k_max, p.rules)


def test_segments() -> None:
    p, bp, e = _first("segments")
    assert not _instance(p, bp).feasible_with()
    wider = replace(p.rules, max_segments=p.rules.max_segments + 1)
    assert _instance(p, bp, rules=wider).feasible_with()
    _assert_lump_exact(p, bp, e)


def test_cash_short() -> None:
    p, bp, e = _first("cash_short")
    _assert_lump_exact(p, bp, e)
    # Some closest-k vector overdraws by exactly the shortfall.
    inst = _instance(p, bp)
    assert any(
        len(v) == e.closest_k and min(inst.replay(v)) == -e.shortfall_cents
        for v in inst.vectors(prune=False)
    )


def test_fee_short() -> None:
    p, bp, e = _first("fee_short")
    _assert_lump_exact(p, bp, e)
    # Some closest-k vector overdraws by less than the shortfall: the fee sets it.
    inst = _instance(p, bp)
    assert any(
        len(v) == e.closest_k and -min(inst.replay(v)) < e.shortfall_cents
        for v in inst.vectors(prune=False)
    )


def test_shortfall_is_exact_against_oracle() -> None:
    checked = 0
    for p, bp, e in _small_points():
        if e is None or e.reason not in ("segments", "cash_short", "fee_short"):
            continue
        try:
            _assert_lump_exact(p, bp, e)
        except oracle.OracleTooBig:
            continue
        checked += 1
    assert checked >= 500


def test_none_exactly_when_feasible() -> None:
    for p, bp, e in _small_points():
        assert (e is None) == is_feasible(p, bp)
        if e is not None:
            assert e.bp == bp


def test_adversarial_reasons_and_lump_dates() -> None:
    """Full-size staircases with tiers: the lump on the binding date is enough."""
    seen = set()
    for case in gen_cases.adversarial_cases(57, 80):
        p = _problem(case)
        for bp in range(100, 10001, 700):
            e = explain(p, bp)
            if e is None or e.shortfall_cents is None or e.reason == "floors":
                continue
            seen.add(e.reason)
            topped = build_problem(
                _with_credit(p.client, e.shortfall_cents, e.binding_date),
                p.rules,
                p.first_payment_date,
                creditor_balance_cents=p.creditor_balance_cents,
                original_balance_cents=p.original_balance_cents,
            )
            assert is_feasible(topped, bp)
    assert {"cash_short", "fee_short"} <= seen


def test_objective_does_not_change_the_answer() -> None:
    for p, bp, e in _small_points()[:600]:
        assert explain(p, bp, "client_first") == e
    p, bp, _ = _first("cash_short")
    with pytest.raises(ValueError):
        explain(p, bp, "cheapest")
