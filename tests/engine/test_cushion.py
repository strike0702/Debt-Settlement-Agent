"""Per-firm soft client cushion (``Problem.min_cushion_cents``, Phase 59 follow-up).

The cushion is Tier C's buffer: the plan keeps every balance at or above it when some
plan at that price can, else the engine falls back to the best plan at 0.

Independent check: requiring b on every balance is the same as starting the client b
lower and requiring 0 (every balance shifts by b, and Φ = min(F, suffix min - b) shifts
the same way). So a kept-cushion result must equal, payment for payment and fee for fee,
the cushion-0 result for the shifted client, whose buffer-0 path is oracle-verified
(``test_search.py``, ``test_objectives.py``). Also: fallback when no plan can keep it,
the scan and rescue amounts unchanged, ``evaluate_offer`` unchanged, every mode.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from feasibility.engine import evaluate, evaluate_offer, is_feasible
from feasibility.models import Client
from feasibility.objectives import CLIENT_FIRST, FEE_FIRST, PROPORTIONAL
from feasibility.problem import Problem, build_problem
from tests.engine import gen_cases
from tests.engine.golden.build_golden import SCENARIOS, scenario_case

MODES = [
    (FEE_FIRST, "front_loaded"),
    (CLIENT_FIRST, "front_loaded"),
    (FEE_FIRST, PROPORTIONAL),
    (CLIENT_FIRST, PROPORTIONAL),
]


def _problem(case: dict, cushion: int = 0, policy: str = "front_loaded",
             client: Client | None = None) -> tuple[Problem, int]:
    c, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client or c, rules, offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
        fee_policy=policy, min_cushion_cents=cushion,
    )
    return problem, offer.settlement_bp


def _cases() -> list[dict]:
    out = [{**scenario_case(n), "settlement_bp": bp} for n in SCENARIOS
           for bp in range(1000, 10001, 1000)]
    return out + gen_cases.random_cases(55, 120)


def _plan(res) -> list[tuple]:
    return [(r.date, r.creditor_payment_cents, r.program_fee_cents) for r in res.schedule]


@pytest.mark.parametrize(("objective", "policy"), MODES)
def test_kept_cushion_equals_shifted_client(objective: str, policy: str) -> None:
    kept = fell_back = 0
    for case in _cases():
        client = gen_cases.case_to_objects(case)[0]
        cushion = max(1, client.draft_amount_cents // 4)
        prob, bp = _problem(case, cushion, policy)
        res = evaluate(prob, bp, objective)
        base = evaluate(_problem(case, 0, policy)[0], bp, objective)
        assert res.feasible == base.feasible == is_feasible(prob, bp)
        if not res.feasible:
            assert res.cushion_kept is None
            assert res.to_dict() == base.to_dict()  # rescue amounts do not move
            continue
        shifted = replace(client, current_balance_cents=client.current_balance_cents - cushion)
        sres = evaluate(_problem(case, 0, policy, shifted)[0], bp, objective)
        if res.cushion_kept:
            kept += 1
            assert sres.feasible and _plan(res) == _plan(sres), case["id"]
            assert all(
                r.balance_cents - s.balance_cents == cushion
                for r, s in zip(res.schedule, sres.schedule, strict=True)
            )
        else:
            fell_back += 1
            assert not sres.feasible, case["id"]  # truly no plan keeps the cushion
            assert _plan(res) == _plan(base)  # the best plan at 0
    assert kept > 50 and fell_back > 5


def test_zero_cushion_reports_none_and_matches_take_home() -> None:
    for case in _cases()[:40]:
        prob, bp = _problem(case)
        res = evaluate(prob, bp)
        assert res.cushion_kept is None
        c, offer, rules = gen_cases.case_to_objects(case)
        assert evaluate_offer(c, offer, rules).to_dict() == res.to_dict()


def test_easy_deal_keeps_a_100_cushion_and_falls_back_at_5000() -> None:
    case = {**scenario_case("easy_deal"), "settlement_bp": 4000}
    res = evaluate(_problem(case, 10_000)[0], 4000)
    assert res.cushion_kept is True
    assert min(r.balance_cents for r in res.schedule) >= 10_000
    big = evaluate(_problem(case, 500_000)[0], 4000)
    assert big.feasible and big.cushion_kept is False
    assert _plan(big) == _plan(evaluate(_problem(case)[0], 4000))


def test_scan_ignores_the_cushion() -> None:
    for name in SCENARIOS:
        case = {**scenario_case(name), "settlement_bp": 100}
        p0, _ = _problem(case)
        p1, _ = _problem(case, 50_000)
        assert [is_feasible(p0, bp) for bp in range(100, 10001, 100)] == [
            is_feasible(p1, bp) for bp in range(100, 10001, 100)
        ]


def test_negative_cushion_rejected() -> None:
    with pytest.raises(ValueError):
        _problem({**scenario_case("easy_deal"), "settlement_bp": 4000}, -1)
