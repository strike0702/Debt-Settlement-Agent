"""Tests for engine_adapter: build_rules, evaluate facts, affordability."""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

import pytest

from app.adapter.engine_adapter import (
    NeedsInfo,
    affordability,
    assumed_fields,
    build_rules,
    clear_affordability_cache,
    evaluate,
)
from app.adapter.validator import validate
from app.domain.belief import BeliefState
from app.domain.scenario import CallScenario, load_scenario
from feasibility.models import CreditorRules, load_case, load_creditor_rules

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "fixtures" / "engine"
DEMO = ROOT / "fixtures" / "demo"
GAP = ENGINE / "gap_curve"


def _fill_required(
    belief: BeliefState,
    *,
    max_payments: int = 6,
    min_payment_cents: int = 2000,
    structure: str = "even",
    strong: bool = True,
) -> None:
    belief.observe(
        "max_payments",
        max_payments,
        "six",
        1,
        verified=strong,
        hedged=not strong,
    )
    belief.observe(
        "min_payment_cents",
        min_payment_cents,
        "twenty",
        1,
        verified=strong,
        hedged=not strong,
    )
    belief.observe(
        "payment_structure",
        structure,
        structure,
        1,
        verified=strong,
        hedged=not strong,
    )


def _scenario_from_engine(case_dir: Path) -> tuple[CallScenario, CreditorRules, date]:
    client, offer, rules = load_case(case_dir)
    scenario = CallScenario(
        id=case_dir.name,
        client=client,
        creditor=offer.creditor,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
        program_fee_pct=rules.program_fee_pct,
        bank_fee_cents=rules.bank_fee_cents,
    )
    fpd = offer.first_payment_date or date(2026, 2, 28)
    return scenario, rules, fpd


def test_build_rules_needs_info_missing() -> None:
    scenario = load_scenario(DEMO)
    belief = BeliefState(scenario.client)
    with pytest.raises(NeedsInfo) as ei:
        build_rules(belief, scenario)
    assert "max_payments" in ei.value.fields


def test_build_rules_needs_info_tentative() -> None:
    scenario = load_scenario(DEMO)
    belief = BeliefState(scenario.client)
    _fill_required(belief, strong=False)
    assert belief.get("max_payments").status.name == "TENTATIVE"
    with pytest.raises(NeedsInfo) as ei:
        build_rules(belief, scenario)
    assert set(ei.value.fields) >= {"max_payments", "min_payment_cents", "payment_structure"}


def test_build_rules_needs_info_contradicted() -> None:
    scenario = load_scenario(DEMO)
    belief = BeliefState(scenario.client)
    _fill_required(belief, strong=True)
    belief.observe("max_payments", 8, "eight", 2, verified=True, hedged=False)
    assert belief.get("max_payments").status.name == "CONTRADICTED"
    with pytest.raises(NeedsInfo) as ei:
        build_rules(belief, scenario)
    assert "max_payments" in ei.value.fields


def test_build_rules_ok() -> None:
    scenario = load_scenario(DEMO)
    belief = BeliefState(scenario.client)
    _fill_required(belief, max_payments=6, min_payment_cents=2000, structure="even")
    rules = build_rules(belief, scenario)
    assert rules.max_payments == 6
    assert rules.max_terms == 6
    assert rules.even_pays is True
    assert rules.is_ballooning_allowed is False
    assert rules.bank_fee_cents == scenario.bank_fee_cents
    assert rules.program_fee_pct == scenario.program_fee_pct
    assert rules.max_token_pays == 6


def test_eval_summary_fact_visibility() -> None:
    scenario, rules, fpd = _scenario_from_engine(ENGINE / "even_ok")
    # even_ok settlement is 55% = 5500 bp
    summary = evaluate(scenario, rules, 5500, fpd)
    assert summary.feasible is True
    assert summary.rows is not None

    public_ids = set(summary.facts.public())
    private_ids = set(summary.facts.private())

    assert "offer_total" in public_ids
    assert "num_payments" in public_ids
    assert "first_payment_date" in public_ids
    assert "last_payment" in public_ids
    assert any(i.startswith("payment_level_") for i in public_ids)

    assert "program_fee" in private_ids
    assert any(i.startswith("balance_") for i in private_ids)
    assert any(i.startswith("program_fee_") for i in private_ids)
    assert any(i.startswith("bank_fee_") for i in private_ids)

    for fact in summary.facts.private().values():
        assert fact.visibility == "PRIVATE"
    for fact in summary.facts.public().values():
        assert fact.visibility == "PUBLIC"


def test_affordability_nonmonotonic_gap_curve() -> None:
    clear_affordability_cache()
    scenario = load_scenario(GAP)
    rules = load_creditor_rules(GAP / "creditor_rules.json")
    fpd = date(2026, 2, 28)
    aff = affordability(scenario, rules, fpd)

    expected = (
        "00000000001111111000111111111111111111111111111111111111111111111111111"
        "00000000000000000000000000000"
    )
    actual = "".join("1" if x else "0" for x in aff.curve)
    assert actual == expected
    assert re.search(r"1+0+1+", actual)
    # Interior gap at 18–20% (bps 1800, 1900, 2000).
    assert aff.curve[17] is False  # 1800
    assert aff.curve[18] is False  # 1900
    assert aff.curve[19] is False  # 2000
    assert aff.curve[16] is True  # 1700
    assert aff.curve[20] is True  # 2100
    assert aff.max_bp == 7100
    assert aff.feasible_bps == [bp for bp, ok in zip(range(100, 10001, 100), aff.curve) if ok]


def test_affordability_max_bp_none_when_nothing_feasible() -> None:
    clear_affordability_cache()
    scenario, _, fpd = _scenario_from_engine(ENGINE / "even_ok")
    rules = CreditorRules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=100000,
        max_token_pays=0,
        min_payment_tiers=[],
        even_pays=True,
        is_ballooning_allowed=False,
        max_segments=1,
        bank_fee_cents=5000,
        program_fee_pct=0.5,
    )
    aff = affordability(scenario, rules, fpd)
    assert aff.max_bp is None
    assert aff.feasible_bps == []
    assert not any(aff.curve)


def test_affordability_demo_timing() -> None:
    """Measure affordability scan on fixtures/demo; recorded in PROGRESS.md."""
    clear_affordability_cache()
    scenario = load_scenario(DEMO)
    belief = BeliefState(scenario.client)
    _fill_required(belief, max_payments=6, min_payment_cents=2000, structure="even")
    rules = build_rules(belief, scenario)
    fpd = belief.get("first_payment_date").value
    assert isinstance(fpd, date)

    t0 = time.perf_counter()
    aff = affordability(scenario, rules, fpd)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    aff2 = affordability(scenario, rules, fpd)
    cached_ms = (time.perf_counter() - t1) * 1000
    assert aff == aff2
    assert elapsed_ms < 5000
    assert cached_ms < elapsed_ms or cached_ms < 5.0
    assert aff.max_bp is not None


@pytest.mark.parametrize(
    "case_name",
    ["even_ok", "balloon_ok", "tier_ok"],
)
def test_validator_passes_engine_feasible(case_name: str) -> None:
    scenario, rules, fpd = _scenario_from_engine(ENGINE / case_name)
    client, offer, _ = load_case(ENGINE / case_name)
    bp = int(round(offer.settlement_pct * 10000))
    summary = evaluate(scenario, rules, bp, fpd)
    assert summary.feasible is True
    assert summary.rows is not None
    violations = validate(
        summary.rows,
        scenario.client,
        summary.offer_total_cents,
        summary.program_fee_cents,
        rules,
        fpd,
    )
    assert violations == []


def test_validator_passes_demo_feasible() -> None:
    scenario = load_scenario(DEMO)
    belief = BeliefState(scenario.client)
    _fill_required(belief, max_payments=6, min_payment_cents=2000, structure="even")
    rules = build_rules(belief, scenario)
    fpd = belief.get("first_payment_date").value
    assert isinstance(fpd, date)
    summary = evaluate(scenario, rules, 2000, fpd, assumed=assumed_fields(belief))
    assert summary.feasible is True
    assert summary.rows is not None
    assert validate(
        summary.rows,
        scenario.client,
        summary.offer_total_cents,
        summary.program_fee_cents,
        rules,
        fpd,
    ) == []
