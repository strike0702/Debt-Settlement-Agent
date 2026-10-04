"""Staggered non-price alt scanners (FPD → min payment → max payments)."""

from __future__ import annotations

from datetime import date

from app.adapter.engine_adapter import (
    affordability,
    build_rules,
    clear_affordability_cache,
)
from app.agent.orchestrator import (
    find_alt_first_payment_date,
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


def test_find_alt_max_payments_skips_useless_plus_one() -> None:
    """4→5 only raises the ceiling to 60%; 6 hits 77%. Jump to 6."""
    clear_affordability_cache()
    sc = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    belief = BeliefState(sc.client)
    for field, val in [
        ("max_payments", 4),
        ("min_payment_cents", 3000),
        ("payment_structure", "balloon"),
    ]:
        belief.observe(field, val, quote=str(val), turn=1, verified=True, hedged=False)
    rules = build_rules(belief, sc)
    fpd = belief.get("first_payment_date").value
    assert isinstance(fpd, date)
    baseline = affordability(sc, rules, fpd)
    assert baseline.max_bp is not None
    alt = find_alt_max_payments(
        sc,
        rules,
        ask_bp=8000,
        fpd=fpd,
        current=4,
        baseline_max_bp=baseline.max_bp,
    )
    assert alt == 6


def test_further_min_alt_when_ceiling_below_ask() -> None:
    """After a weak min unlock (8%), a deeper cut can raise the ceiling to the ask."""
    clear_affordability_cache()
    sc = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    belief = BeliefState(sc.client)
    for field, val in [
        ("max_payments", 5),
        ("min_payment_cents", 5000),
        ("payment_structure", "balloon"),
    ]:
        belief.observe(field, val, quote=str(val), turn=1, verified=True, hedged=False)
    rules = build_rules(belief, sc)
    fpd = belief.get("first_payment_date").value
    assert isinstance(fpd, date)

    # Useless max_payments bump that does not raise the ceiling must be ignored.
    assert (
        find_alt_max_payments(
            sc,
            rules,
            ask_bp=5000,
            fpd=fpd,
            current=5,
            baseline_max_bp=800,
        )
        is None
    )

    alt = find_next_term_alt(
        sc,
        rules,
        ask_bp=5000,
        fpd=fpd,
        max_payments=5,
        min_payment_cents=5000,
        terms_countered=[
            f"first_payment_date:{fpd.isoformat()}",
            "min_payment_cents:5000",
        ],
        baseline_max_bp=800,
    )
    assert alt == ("min_payment_cents", 3000)


def test_fpd_alt_prefers_higher_ceiling_when_ask_infeasible() -> None:
    """Current month at a $50 floor is only 8%; a later month reaches ~77%."""
    clear_affordability_cache()
    sc = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    belief = BeliefState(sc.client)
    for field, val in [
        ("max_payments", 5),
        ("min_payment_cents", 5000),
        ("payment_structure", "balloon"),
    ]:
        belief.observe(field, val, quote=str(val), turn=1, verified=True, hedged=False)
    rules = build_rules(belief, sc)
    fpd = belief.get("first_payment_date").value
    assert isinstance(fpd, date)
    current = affordability(sc, rules, fpd)
    alt = find_alt_first_payment_date(
        sc, rules, ask_bp=8000, requested=fpd, already=set()
    )
    assert alt is not None and alt != fpd
    raised = affordability(sc, rules, alt)
    assert current.max_bp is not None and raised.max_bp is not None
    assert raised.max_bp > current.max_bp
    assert raised.max_bp >= 6000
