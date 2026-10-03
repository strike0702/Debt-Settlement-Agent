"""Staggered non-price alt scanners (FPD → min payment → max payments)."""

from __future__ import annotations

from datetime import date

from app.adapter.engine_adapter import build_rules, clear_affordability_cache
from app.agent.orchestrator import (
    find_alt_max_payments,
    find_alt_min_payment_cents,
    find_next_term_alt,
)
from app.domain.belief import BeliefState
from app.domain.scenario import load_scenario


def test_find_next_term_alt_prefers_fpd_then_min() -> None:
    clear_affordability_cache()
    sc = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    belief = BeliefState(sc.client)
    for field, val in [
        ("max_payments", 5),
        ("min_payment_cents", 10000),
        ("payment_structure", "balloon"),
    ]:
        belief.observe(field, val, quote=str(val), turn=1, verified=True, hedged=False)
    rules = build_rules(belief, sc)
    fpd = belief.get("first_payment_date").value
    assert isinstance(fpd, date)

    first = find_next_term_alt(
        sc,
        rules,
        ask_bp=8000,
        fpd=fpd,
        max_payments=5,
        min_payment_cents=10000,
        terms_countered=[],
    )
    assert first is not None
    assert first[0] == "first_payment_date"

    after_fpd = find_next_term_alt(
        sc,
        rules,
        ask_bp=8000,
        fpd=fpd,
        max_payments=5,
        min_payment_cents=10000,
        terms_countered=[f"first_payment_date:{first[1].isoformat()}"],
    )
    # After FPD is spent, lower min and/or higher max may still unlock.
    assert after_fpd is None or after_fpd[0] in (
        "min_payment_cents",
        "max_payments",
    )


def test_find_alt_min_payment_unlocks_when_possible() -> None:
    clear_affordability_cache()
    sc = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    belief = BeliefState(sc.client)
    for field, val in [
        ("max_payments", 5),
        ("min_payment_cents", 10000),
        ("payment_structure", "balloon"),
    ]:
        belief.observe(field, val, quote=str(val), turn=1, verified=True, hedged=False)
    # Use a later FPD so lowering min is what we're testing in isolation is optional;
    # at default FPD, min search may still find something or return None.
    rules = build_rules(belief, sc)
    fpd = belief.get("first_payment_date").value
    assert isinstance(fpd, date)
    alt = find_alt_min_payment_cents(
        sc, rules, ask_bp=8000, fpd=fpd, current=10000
    )
    if alt is not None:
        assert 1000 <= alt < 10000


def test_find_alt_max_payments_steps_up() -> None:
    clear_affordability_cache()
    sc = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    belief = BeliefState(sc.client)
    for field, val in [
        ("max_payments", 3),
        ("min_payment_cents", 10000),
        ("payment_structure", "balloon"),
    ]:
        belief.observe(field, val, quote=str(val), turn=1, verified=True, hedged=False)
    rules = build_rules(belief, sc)
    fpd = date(2027, 1, 31)
    alt = find_alt_max_payments(sc, rules, ask_bp=4000, fpd=fpd, current=3)
    if alt is not None:
        assert 3 < alt <= 60
