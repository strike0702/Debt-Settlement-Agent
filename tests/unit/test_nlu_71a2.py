"""Phase 71a-2 NLU fixes: offer vs counter, amount readers, salvage, numbers, private cue.

- Offer vs counter (one rule, both schemas): a price is ``offer`` only when the
  rep speaks first; after any agent line it is ``counter``.
- Acts converter: a bare "$350" beside an exact payment count is ambiguous, not
  a minimum; a "$900 total" next to a percent the model left out is the total.
- A reply cut at the token cap keeps its complete acts instead of ``unclear``.
- "first two" is an ordinal and a count; "one second" is a pause, not a figure.
- "left after rent" / "disposable" / "spare each month" are private-info asks.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agent.nlu import convert_acts, parse_acts, post_verify
from app.agent.nlu.cues import repair_asks_client_private_info
from app.agent.nlu_types import TurnAnalysis
from app.agent.numbers import extract_tokens, words_to_number
from app.domain.nlu_types import ActList

_REF = date(2026, 4, 1)
_ASK = "What settlement terms can you accept on this account?"


def _acts(*acts: dict) -> ActList:
    return ActList.model_validate({"acts": list(acts)})


def test_a_price_is_an_offer_only_when_the_rep_speaks_first() -> None:
    line = "We'd settle for $900 total."
    flat = TurnAnalysis(
        stance="offer", settlement_ask_total_cents=90000, ask_total_quote="$900 total"
    )
    assert post_verify(flat, line, ref=_REF, last_agent_line=_ASK).stance == "counter"
    assert post_verify(flat, line, ref=_REF, last_agent_line="").stance == "offer"
    price = _acts({"type": "price_ask", "total_cents": 90000, "quote": "$900 total"})
    assert convert_acts(price, line, last_agent_line=_ASK, ref=_REF).stance == "counter"
    assert convert_acts(price, line, last_agent_line="", ref=_REF).stance == "offer"


def test_bare_amount_beside_an_exact_count_is_ambiguous_not_a_minimum() -> None:
    line = "Three payments, $350."
    acts = _acts(
        {"type": "state_rule", "field": "max_payments", "value": 3, "quote": "Three payments"},
        {"type": "state_rule", "field": "min_payment_cents", "value": 35000, "quote": "$350"},
    )
    out = convert_acts(acts, line, last_agent_line=_ASK, ref=_REF)
    assert [t.field for t in out.terms] == ["max_payments"]
    assert out.amount_ambiguous_cents == 35000 and out.unaccounted_numbers == []
    # A per-payment cue keeps it a minimum.
    each = _acts(
        {"type": "state_rule", "field": "min_payment_cents", "value": 35000, "quote": "$350"}
    )
    kept = convert_acts(each, "Three payments, $350 each.", last_agent_line=_ASK, ref=_REF)
    assert [t.field for t in kept.terms] == ["min_payment_cents"]


def test_total_next_to_a_percent_is_read_when_the_model_drops_it() -> None:
    line = "Fifty percent, which comes to $900 total."
    acts = _acts({"type": "price_ask", "pct": 50, "quote": "Fifty percent"})
    out = convert_acts(acts, line, last_agent_line=_ASK, ref=_REF)
    assert (out.settlement_ask_pct, out.settlement_ask_total_cents) == (50.0, 90000)
    assert out.unaccounted_numbers == []
    balance = convert_acts(
        acts, "Fifty percent of the $4,000 balance in total.", last_agent_line=_ASK, ref=_REF
    )
    assert balance.settlement_ask_total_cents is None
    tiers = _acts({"type": "rule_item", "quote": "the first two payments at least $190"})
    tier_line = "In total, the first two payments at least $190."
    tier_out = convert_acts(tiers, tier_line, last_agent_line=_ASK, ref=_REF)
    assert tier_out.terms and tier_out.settlement_ask_total_cents is None


def test_a_reply_cut_at_the_token_cap_keeps_its_complete_acts() -> None:
    reply = (
        '{"acts":[{"type":"state_rule","field":"max_payments","value":3,'
        '"quote":"3 even payments"},{"type":"price_ask","total_cents":42000,"quo'
    )
    acts, error = parse_acts(reply)
    assert error is not None and error.startswith("salvaged 1")
    assert [a.type for a in acts.acts] == ["state_rule"]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("the first two payments", [("ordinal", 1), ("count", 2)]),
        ("hold on one second", []),
        ("give me a minute", []),
        ("twenty first", [("count", 21)]),
    ],
)
def test_ordinal_and_count_are_two_tokens_and_pauses_are_not_figures(
    text: str, want: list[tuple[str, int]]
) -> None:
    assert [(t.kind, t.value) for t in extract_tokens(text, ref=_REF)] == want


def test_words_to_number_refuses_ordinal_plus_count() -> None:
    assert words_to_number("first two") is None
    assert words_to_number("one second") is None
    assert words_to_number("twenty first") == 21


@pytest.mark.parametrize(
    ("text", "private"),
    [
        ("Roughly what does the client have left after rent each month?", True),
        ("How much is there after bills?", True),
        ("What's their disposable income?", True),
        ("How much can they spare each month?", True),
        ("What is left on the balance after the first payment?", False),
        ("We don't care what they have left after rent, fifty percent.", False),
    ],
)
def test_private_cue_covers_money_left_after_living_costs(text: str, private: bool) -> None:
    assert repair_asks_client_private_info(False, text) is private
