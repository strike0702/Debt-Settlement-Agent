"""Phase 49 small fixes: singular payment ack (46b.4) and the ``ack_total`` meaning (46b.1)."""

from __future__ import annotations

from datetime import date

import pytest

from app.agent.nlg import ack_template, ack_variants, render_acts
from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact
from app.llm.prompts import PLACEHOLDER_MEANINGS

_REF = date(2026, 3, 1)


def _ack_action(count: int, *, with_min: bool = False) -> Action:
    ack = {
        "ack_max_payments": Fact(
            id="ack_max_payments", kind="count", value=count, visibility="PUBLIC", source="creditor"
        )
    }
    if with_min:
        ack["ack_min_payment"] = Fact(
            id="ack_min_payment", kind="money", value=25000, visibility="PUBLIC", source="creditor"
        )
    return Action(intent=Intent.ASK, next_phase=Phase.DISCOVERY, ack=ack)


@pytest.mark.parametrize("turn", [0, 1, 2])
@pytest.mark.parametrize("h3", [False, True])
def test_count_of_one_is_acked_in_the_singular(turn: int, h3: bool) -> None:
    out = render_acts(
        _ack_action(1), _REF, creditor_numbers={("count", 1)}, turn=turn, code_ack=not h3
    )
    assert len(out) == 1
    assert "1 payment" in out[0] and "1 payments" not in out[0]


def test_count_of_one_with_a_minimum() -> None:
    out = render_acts(
        _ack_action(1, with_min=True),
        _REF,
        creditor_numbers={("count", 1), ("money", 25000)},
        turn=1,
        code_ack=True,
    )
    assert out == ["Understood, up to 1 payment, at least $250 each."]


def test_plural_counts_unchanged() -> None:
    out = render_acts(_ack_action(6), _REF, creditor_numbers={("count", 6)}, turn=0, code_ack=True)
    assert out == ["Got it, up to 6 payments."]
    assert ack_template(("ack_max_payments",), 0) == "Got it, up to {ack_max_payments} payments."
    singular = ack_template(("ack_max_payments",), 0, singular=True)
    assert singular == "Got it, up to {ack_max_payments} payment."
    assert ack_variants(("ack_max_payments",))  # plural bank of wordings untouched


def test_ack_total_has_a_placeholder_meaning() -> None:
    assert "ack_total" in PLACEHOLDER_MEANINGS
    assert "total" in PLACEHOLDER_MEANINGS["ack_total"]
