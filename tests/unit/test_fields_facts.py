"""Field registry defaults and Fact visibility."""

from __future__ import annotations

from datetime import date

from app.domain.belief import BeliefState, TermStatus
from app.domain.facts import Fact, FactSet
from app.domain.fields import FIELD_REGISTRY, FIELDS_BY_NAME, REQUIRED_FIELDS
from app.domain.scenario import load_scenario


def test_field_registry_defaults() -> None:
    assert REQUIRED_FIELDS == ["max_payments", "min_payment_cents", "payment_structure"]

    max_segments = FIELDS_BY_NAME["max_segments"]
    assert max_segments.required is False
    assert max_segments.default_factory is not None
    assert max_segments.default_factory() == 2

    tiers = FIELDS_BY_NAME["min_payment_tiers"]
    assert tiers.required is False
    assert tiers.default_factory is not None
    assert tiers.default_factory() == []

    fpd = FIELDS_BY_NAME["first_payment_date"]
    assert fpd.required is False
    assert fpd.default_factory is None  # seeded from client in BeliefState

    token = FIELDS_BY_NAME["max_token_pays"]
    assert token.required is False
    assert token.default_factory is None  # synced to max_payments while ASSUMED

    assert FIELDS_BY_NAME["max_payments"].prior_range == (1, 60)
    assert FIELDS_BY_NAME["min_payment_cents"].prior_range == (1000, 100000)
    assert FIELDS_BY_NAME["payment_structure"].prior_range == (
        "even",
        "balloon",
        "flexible",
    )
    assert len(FIELD_REGISTRY) == 7


def test_belief_seeds_assumed_defaults() -> None:
    scenario = load_scenario("fixtures/demo")
    belief = BeliefState(scenario.client)

    assert belief.get("max_segments").status == TermStatus.ASSUMED
    assert belief.get("max_segments").value == 2
    assert belief.get("min_payment_tiers").status == TermStatus.ASSUMED
    assert belief.get("min_payment_tiers").value == []
    assert belief.get("first_payment_date").status == TermStatus.ASSUMED
    assert belief.get("first_payment_date").value == date(2026, 3, 31)
    assert belief.get("max_token_pays").status == TermStatus.ASSUMED
    assert belief.usable_for_engine("max_segments")
    assert not belief.usable_for_engine("max_payments")


def test_fact_visibility_and_render() -> None:
    ref = date(2026, 6, 1)
    public = Fact(
        id="offer_total",
        kind="money",
        value=62500,
        visibility="PUBLIC",
        source="engine",
    )
    private = Fact(
        id="max_affordable_bp",
        kind="pct",
        value=5500,
        visibility="PRIVATE",
        source="engine",
    )
    schedule_date = Fact(
        id="first_payment_date",
        kind="date",
        value=date(2026, 4, 30),
        visibility="PUBLIC",
        source="creditor",
    )
    count = Fact(
        id="num_payments",
        kind="count",
        value=6,
        visibility="PUBLIC",
        source="engine",
    )

    assert public.visibility == "PUBLIC"
    assert private.visibility == "PRIVATE"
    assert public.render(ref) == "$625"
    assert private.render(ref) == "55%"
    assert schedule_date.render(ref) == "April 30"
    assert count.render(ref) == "6"

    fs = FactSet()
    fs.add(public)
    fs.add(private)
    fs.add(schedule_date)
    fs.add(count)
    assert set(fs.public()) == {"offer_total", "first_payment_date", "num_payments"}
    assert set(fs.private()) == {"max_affordable_bp"}
    assert "offer_total" in fs
    assert fs["offer_total"].value == 62500


def test_private_fact_ids_enforced() -> None:
    """F27: PRIVATE_FACT_IDS registry rejects PUBLIC tagging."""
    import pytest
    from app.domain.facts import Fact, FactSet
    fs = FactSet()
    with pytest.raises(ValueError, match="must be PRIVATE"):
        fs.add(
            Fact(
                id="program_fee",
                kind="money",
                value=100,
                visibility="PUBLIC",
                source="engine",
            )
        )
