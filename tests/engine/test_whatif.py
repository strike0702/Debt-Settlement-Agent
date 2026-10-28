"""What-if on creditor terms (``feasibility/whatif.py``) against rebuilt problems.

- Every change gives the same feasible set as a ``Problem`` built from scratch with the
  changed rules or date (the adapter's affordability scan), on the 7 scenarios and on
  seeded random cases; rules changes reuse the base simulation, a date change rebuilds.
- ``max_bp`` is the top of that set; the tail-cap cut never drops a feasible point.
- ``unlocking_changes`` lists exactly the grid values under which the ask fits, field by
  field in the agent's order, closest date first, never the current value.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.scenario import load_scenario, resolve_scenario_dir
from feasibility.engine import is_feasible
from feasibility.models import CreditorRules
from feasibility.problem import Problem, build_problem
from feasibility.whatif import (
    FIELDS,
    SCAN_BPS,
    TermChange,
    candidates,
    changed_rules,
    structure_of,
    total_cap,
    unlocking_changes,
    what_if,
)
from tests.engine import gen_cases

SCENARIOS = (
    "balloon_structure", "counter_ladder", "easy_deal", "haggling_rep", "late_start_date",
    "no_space", "rescue_escalate",
)


def _scenario_problem(sid: str, rules: CreditorRules) -> Problem:
    sc = load_scenario(resolve_scenario_dir(sid), rebase_to=date(2026, 10, 7))
    return build_problem(
        sc.client, rules, None,
        creditor_balance_cents=sc.creditor_balance_cents,
        original_balance_cents=sc.original_balance_cents,
    )


def _rules(**kw) -> CreditorRules:
    base = dict(
        max_terms=5, max_payments=5, min_payment_cents=5000, max_token_pays=5,
        min_payment_tiers=[], even_pays=False, is_ballooning_allowed=False, max_segments=2,
        bank_fee_cents=1000, program_fee_bp=2000,
    )
    base.update(kw)
    return CreditorRules(**base)


def _random_problems(n: int) -> list[Problem]:
    out = []
    for case in gen_cases.random_cases(3, n):
        client, offer, rules = gen_cases.case_to_objects(case)
        out.append(build_problem(
            client, rules, offer.first_payment_date,
            creditor_balance_cents=offer.creditor_balance_cents,
            original_balance_cents=offer.original_balance_cents,
        ))
    return out


def _rebuilt(p: Problem, change: TermChange) -> Problem:
    if change.field == "first_payment_date":
        rules, fpd = p.rules, change.value
    else:
        rules, fpd = changed_rules(p.rules, change), p.first_payment_date
    return build_problem(
        p.client, rules, fpd,  # type: ignore[arg-type]
        creditor_balance_cents=p.creditor_balance_cents,
        original_balance_cents=p.original_balance_cents,
    )


def _problems() -> list[Problem]:
    out = [
        _scenario_problem(sid, _rules(**kw))
        for sid in SCENARIOS
        for kw in ({}, {"is_ballooning_allowed": True}, {"even_pays": True})
    ]
    return out + _random_problems(40)


@pytest.mark.parametrize("field", FIELDS)
def test_what_if_matches_a_rebuilt_problem(field: str) -> None:
    seen = 0
    for p in _problems():
        for change in candidates(p, field)[:4]:  # type: ignore[arg-type]
            w = what_if(p, change)
            fresh = _rebuilt(p, change)
            expect = [bp for bp in SCAN_BPS if is_feasible(fresh, bp)]
            assert w.feasible_bps == expect
            assert w.max_bp == (expect[-1] if expect else None)
            assert (w.problem.base is p.base) == (field != "first_payment_date")
            seen += 1
    assert seen >= 20


def test_tail_cap_never_cuts_a_feasible_point() -> None:
    for p in _problems():
        cap = total_cap(p)
        for bp in SCAN_BPS:
            if is_feasible(p, bp):
                assert p.offer_total(bp) <= cap


def test_candidate_grid() -> None:
    p = _scenario_problem("balloon_structure", _rules(min_payment_cents=5000, max_payments=4))
    mins = [c.value for c in candidates(p, "min_payment_cents")]
    assert mins == [4000, 3000, 2000, 1000]
    maxes = [c.value for c in candidates(p, "max_payments")]
    assert maxes[0] == 5 and len(maxes) == 20
    dates = [c.value for c in candidates(p, "first_payment_date")]
    assert dates == sorted(dates) and all(d <= p.client.last_draft_date for d in dates)
    assert {c.value for c in candidates(p, "payment_structure")} == {"even", "balloon"}
    assert structure_of(p.rules) == "flexible"
    assert candidates(_scenario_problem("easy_deal", _rules(even_pays=True)), "max_segments") == []


def test_max_payments_keeps_no_token_limit() -> None:
    r = changed_rules(_rules(max_token_pays=5), TermChange("max_payments", 8))
    assert (r.max_payments, r.max_terms, r.max_token_pays) == (8, 8, 8)
    r = changed_rules(_rules(max_token_pays=2), TermChange("max_payments", 8))
    assert r.max_token_pays == 2


def test_unlocking_changes_are_exactly_the_ones_that_fit() -> None:
    hits = 0
    for p in _problems()[:30]:
        for ask in (3000, 5000, 7000):
            if is_feasible(p, ask):
                continue
            got = unlocking_changes(p, ask)
            listed = {(w.change.field, w.change.value) for w in got}
            for field in FIELDS:
                for change in candidates(p, field):
                    if change.value == p.first_payment_date:  # the current date
                        assert (field, change.value) not in listed
                        continue
                    fits = is_feasible(_rebuilt(p, change), ask)
                    assert ((field, change.value) in listed) == fits
            order = [FIELDS.index(w.change.field) for w in got]
            assert order == sorted(order)
            dates = [w.change.value for w in got if w.change.field == "first_payment_date"]
            dist = [abs((d - p.first_payment_date).days) for d in dates]  # type: ignore[operator]
            assert dist == sorted(dist)
            hits += len(got)
    assert hits > 0
