"""Prefix-cap identity: per-candidate simulation == base simulation minus prefix debits.

For every candidate the engine generates on every golden-corpus input (all 1305
offers, including each scenario at all 100 bp), the shortcut
``simulate.apply_payments(base, ...)`` must equal a full ``simulate`` replay:
same balances on every date, same window minima, same pre-window minimum. It also
checks the identity explicitly: ``balance(d) = base(d) - P_min(j,k) - bf*min(j,k)``.
This is the fact ``problem.py`` documents and engine v2's exact search relies on.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from feasibility.problem import Problem, build_problem
from feasibility.shapes import generate_candidates
from feasibility.simulate import simulate
from tests.engine import gen_cases
from tests.engine.golden.build_golden import GOLDEN, expand

RECORDS = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line]


def _problem(case: dict) -> tuple[Problem, int]:
    client, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    return problem, problem.offer_total(offer.settlement_bp)


def _window_index(cadence: list[date], d: date) -> int:
    return sum(1 for t in cadence if t <= d)


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_prefix_cap_identity(record: dict) -> None:
    checked = 0
    for case in expand(record):
        problem, S = _problem(case)
        if problem.k_max == 0:
            continue
        cadence = problem.cadence
        bf = problem.rules.bank_fee_cents
        for cand in generate_candidates(S, problem.k_max, problem.floors, problem.rules):
            full = simulate(problem.client, list(zip(cadence, cand.payments)), cadence, bf)
            fast = problem.simulate_vector(cand.payments)
            assert fast == full, (case["id"], cand)
            prefix = [0]
            for p in cand.payments:
                prefix.append(prefix[-1] + p)
            for d, bal in full.balances.items():
                j = min(_window_index(cadence, d), cand.k)
                assert bal == problem.base.balances[d] - prefix[j] - bf * j
            for j in range(1, problem.M + 1):
                jk = min(j, cand.k)
                assert full.w[j] == problem.base_w[j] - prefix[jk] - bf * jk
            assert full.pre_min == problem.pre_min
            checked += 1
    total_checked[record["id"]] = checked


total_checked: dict[str, int] = {}


def test_prefix_cap_covered_many_candidates() -> None:
    """Guard against a silently empty sweep (runs after the parametrized cases)."""
    if len(total_checked) < len(RECORDS):
        pytest.skip("run the full module to count coverage")
    assert sum(total_checked.values()) > 10_000
