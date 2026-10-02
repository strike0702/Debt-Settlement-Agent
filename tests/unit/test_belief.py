"""Table-driven BeliefState transition tests (PLAN section 4.4)."""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.belief import BeliefState, TermStatus
from app.domain.scenario import load_scenario


def _fresh() -> BeliefState:
    return BeliefState(load_scenario("fixtures/demo").client)


def _observe(
    belief: BeliefState,
    field: str,
    value: Any,
    *,
    verified: bool,
    hedged: bool,
    turn: int = 1,
    quote: str = "quote",
):
    return belief.observe(
        field, value, quote, turn, verified=verified, hedged=hedged
    )


@pytest.mark.parametrize(
    "case",
    [
        {
            "id": "unknown_strong_to_known",
            "setup": [],
            "action": ("observe", "max_payments", 6, True, False),
            "expect_status": TermStatus.KNOWN,
            "expect_value": 6,
        },
        {
            "id": "unknown_weak_unverified_to_tentative",
            "setup": [],
            "action": ("observe", "max_payments", 6, False, False),
            "expect_status": TermStatus.TENTATIVE,
            "expect_value": 6,
        },
        {
            "id": "unknown_weak_hedged_to_tentative",
            "setup": [],
            "action": ("observe", "max_payments", 6, True, True),
            "expect_status": TermStatus.TENTATIVE,
            "expect_value": 6,
        },
        {
            "id": "assumed_strong_to_known",
            "setup": [],
            "action": ("observe", "max_segments", 3, True, False),
            "expect_status": TermStatus.KNOWN,
            "expect_value": 3,
        },
        {
            "id": "assumed_weak_to_tentative",
            "setup": [],
            "action": ("observe", "max_segments", 3, False, False),
            "expect_status": TermStatus.TENTATIVE,
            "expect_value": 3,
        },
        {
            "id": "tentative_same_strong_promotes",
            "setup": [("observe", "max_payments", 6, False, False)],
            "action": ("observe", "max_payments", 6, True, False),
            "expect_status": TermStatus.KNOWN,
            "expect_value": 6,
        },
        {
            "id": "tentative_same_weak_stays",
            "setup": [("observe", "max_payments", 6, False, False)],
            "action": ("observe", "max_payments", 6, False, False),
            "expect_status": TermStatus.TENTATIVE,
            "expect_value": 6,
        },
        {
            "id": "known_same_stays_known",
            "setup": [("observe", "max_payments", 6, True, False)],
            "action": ("observe", "max_payments", 6, True, False),
            "expect_status": TermStatus.KNOWN,
            "expect_value": 6,
        },
        {
            "id": "tentative_different_self_correct_strong",
            "setup": [("observe", "max_payments", 6, False, False)],
            "action": ("observe", "max_payments", 8, True, False),
            "expect_status": TermStatus.KNOWN,
            "expect_value": 8,
        },
        {
            "id": "tentative_different_self_correct_weak",
            "setup": [("observe", "max_payments", 6, False, False)],
            "action": ("observe", "max_payments", 8, False, False),
            "expect_status": TermStatus.TENTATIVE,
            "expect_value": 8,
        },
        {
            "id": "known_different_to_contradicted",
            "setup": [("observe", "max_payments", 6, True, False)],
            "action": ("observe", "max_payments", 8, True, False),
            "expect_status": TermStatus.CONTRADICTED,
            "expect_value": 8,
            "expect_history": [6],
        },
        {
            "id": "contradicted_strong_to_known",
            "setup": [
                ("observe", "max_payments", 6, True, False),
                ("observe", "max_payments", 8, True, False),
            ],
            "action": ("observe", "max_payments", 7, True, False),
            "expect_status": TermStatus.KNOWN,
            "expect_value": 7,
        },
        {
            "id": "contradicted_weak_to_tentative",
            "setup": [
                ("observe", "max_payments", 6, True, False),
                ("observe", "max_payments", 8, True, False),
            ],
            "action": ("observe", "max_payments", 7, False, False),
            "expect_status": TermStatus.TENTATIVE,
            "expect_value": 7,
        },
        {
            "id": "confirm_readback_yes",
            "setup": [("observe", "max_payments", 6, False, False)],
            "action": ("confirm_readback", "max_payments", True),
            "expect_status": TermStatus.KNOWN,
            "expect_value": 6,
        },
        {
            "id": "confirm_readback_no",
            "setup": [("observe", "max_payments", 6, False, False)],
            "action": ("confirm_readback", "max_payments", False),
            "expect_status": TermStatus.UNKNOWN,
            "expect_value": None,
        },
    ],
    ids=lambda c: c["id"],
)
def test_belief_transitions(case: dict[str, Any]) -> None:
    belief = _fresh()
    for step in case["setup"]:
        kind = step[0]
        if kind == "observe":
            _, field, value, verified, hedged = step
            _observe(belief, field, value, verified=verified, hedged=hedged)
        else:
            raise AssertionError(f"bad setup step: {step}")

    action = case["action"]
    if action[0] == "observe":
        _, field, value, verified, hedged = action
        change = _observe(belief, field, value, verified=verified, hedged=hedged)
    elif action[0] == "confirm_readback":
        _, field, yes = action
        change = belief.confirm_readback(field, yes)
    else:
        raise AssertionError(f"bad action: {action}")

    term = belief.get(case["action"][1])
    assert term.status == case["expect_status"]
    assert term.value == case["expect_value"]
    assert change.new_status == case["expect_status"]
    assert change.new_value == case["expect_value"]
    if "expect_history" in case:
        assert term.history == case["expect_history"]


def test_usable_for_engine_only_known_or_assumed() -> None:
    belief = _fresh()
    assert belief.usable_for_engine("max_segments")  # ASSUMED
    assert not belief.usable_for_engine("max_payments")  # UNKNOWN

    _observe(belief, "max_payments", 6, verified=False, hedged=False)
    assert not belief.usable_for_engine("max_payments")  # TENTATIVE

    belief.confirm_readback("max_payments", True)
    assert belief.usable_for_engine("max_payments")  # KNOWN

    _observe(belief, "min_payment_cents", 2500, verified=True, hedged=False)
    _observe(belief, "min_payment_cents", 3000, verified=True, hedged=False)
    assert belief.get("min_payment_cents").status == TermStatus.CONTRADICTED
    assert not belief.usable_for_engine("min_payment_cents")


def test_max_token_pays_syncs_from_max_payments() -> None:
    belief = _fresh()
    assert belief.get("max_token_pays").status == TermStatus.ASSUMED
    assert belief.get("max_token_pays").value is None
    _observe(belief, "max_payments", 6, verified=True, hedged=False)
    assert belief.get("max_token_pays").value == 6
    assert belief.get("max_token_pays").status == TermStatus.ASSUMED


def test_contradicted_same_value_resolves_to_known() -> None:
    """Clarify answer that repeats the new value must clear CONTRADICTED."""
    belief = _fresh()
    _observe(belief, "max_payments", 8, verified=True, hedged=False)
    _observe(belief, "max_payments", 3, verified=True, hedged=False)
    assert belief.get("max_payments").status == TermStatus.CONTRADICTED
    _observe(belief, "max_payments", 3, verified=True, hedged=False)
    assert belief.get("max_payments").status == TermStatus.KNOWN
    assert belief.get("max_payments").value == 3


def test_contradicted_old_value_resolves_to_known() -> None:
    """Clarify answer that restates the original value → KNOWN old value."""
    belief = _fresh()
    _observe(belief, "max_payments", 8, verified=True, hedged=False)
    _observe(belief, "max_payments", 3, verified=True, hedged=False)
    _observe(belief, "max_payments", 8, verified=True, hedged=False)
    assert belief.get("max_payments").status == TermStatus.KNOWN
    assert belief.get("max_payments").value == 8


def test_contradicted_hedged_resolves_to_tentative() -> None:
    belief = _fresh()
    _observe(belief, "max_payments", 8, verified=True, hedged=False)
    _observe(belief, "max_payments", 3, verified=True, hedged=False)
    _observe(belief, "max_payments", 3, verified=True, hedged=True)
    assert belief.get("max_payments").status == TermStatus.TENTATIVE
    assert belief.get("max_payments").value == 3


def test_load_demo_scenario() -> None:
    scenario = load_scenario("fixtures/demo")
    assert scenario.id == "demo"
    assert scenario.creditor == "NorthPeak Collections"
    assert scenario.creditor_balance_cents == 125000
    assert scenario.original_balance_cents == 160000
    assert scenario.program_fee_pct == 0.18
    assert scenario.bank_fee_cents == 950
    assert scenario.client.draft_amount_cents == 22000
