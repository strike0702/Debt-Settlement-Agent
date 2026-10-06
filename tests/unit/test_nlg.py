"""NLG deterministic templates pass both guards for every intent."""

from __future__ import annotations

from datetime import date

import pytest

from app.agent.acts import ANSWER_POINTS
from app.agent.nlg import SAFE_FALLBACK, TEMPLATES, render_action
from app.agent.policy import Action, Intent, Phase, opening_action
from app.domain.facts import Fact

_REF = date(2026, 3, 1)


def _money(fid: str, cents: int) -> Fact:
    return Fact(id=fid, kind="money", value=cents, visibility="PUBLIC", source="engine")


def _pct(fid: str, bp: int) -> Fact:
    return Fact(id=fid, kind="pct", value=bp, visibility="PUBLIC", source="engine")


def _count(fid: str, n: int) -> Fact:
    return Fact(id=fid, kind="count", value=n, visibility="PUBLIC", source="engine")


def _date(fid: str, d: date) -> Fact:
    return Fact(id=fid, kind="date", value=d, visibility="PUBLIC", source="engine")


def _sample_action(intent: Intent) -> Action:
    """Minimal valid Action + facts/slots so the template fills cleanly."""
    common_next = Phase.DISCOVERY
    if intent == Intent.OPENING:
        return opening_action(
            firm_name="Synthetic Debt Relief",
            opening_disclosure=(
                "This call uses synthetic data for demonstration only."
            ),
        )
    if intent == Intent.ASK:
        return Action(
            intent=intent,
            text_slots={
                "ask_text": "What's the most payments they'll take?",
                "field_label": "maximum number of payments",
            },
            next_phase=common_next,
        )
    if intent == Intent.ASK_SETTLEMENT:
        return Action(intent=intent, next_phase=common_next)
    if intent == Intent.READ_BACK:
        return Action(
            intent=intent,
            facts={"readback_value": _count("readback_value", 6)},
            text_slots={"field_label": "maximum number of payments"},
            required={"readback_value"},
            next_phase=common_next,
        )
    if intent == Intent.CLARIFY:
        return Action(
            intent=intent,
            facts={
                "clarify_old": _count("clarify_old", 6),
                "clarify_new": _count("clarify_new", 8),
            },
            text_slots={"field_label": "maximum number of payments"},
            required={"clarify_old", "clarify_new"},
            next_phase=common_next,
        )
    if intent == Intent.REFUSE_PRIVATE:
        return Action(intent=intent, next_phase=common_next)
    if intent == Intent.REFUSE_COMMIT:
        return Action(intent=intent, next_phase=common_next)
    if intent == Intent.COUNTER:
        return Action(
            intent=intent,
            facts={
                "counter_pct": _pct("counter_pct", 4000),
                "offer_total": _money("offer_total", 50_000),
            },
            required={"counter_pct", "offer_total"},
            next_phase=Phase.NEGOTIATE,
        )
    if intent == Intent.COUNTER_TERMS:
        return Action(
            intent=intent,
            facts={
                "alt_first_payment_date": _date(
                    "alt_first_payment_date", date(2026, 6, 30)
                ),
            },
            required={"alt_first_payment_date"},
            next_phase=Phase.NEGOTIATE,
        )
    if intent == Intent.CONFIRM_SCHEDULE:
        return Action(
            intent=intent,
            facts={
                "settlement_pct": _pct("settlement_pct", 4500),
                "num_payments": _count("num_payments", 6),
                "offer_total": _money("offer_total", 75_000),
                "first_payment_date": _date("first_payment_date", date(2026, 4, 15)),
            },
            required={
                "settlement_pct",
                "num_payments",
                "offer_total",
                "first_payment_date",
            },
            next_phase=Phase.CONFIRM,
        )
    if intent == Intent.SPEAK_SCHEDULE:
        return Action(
            intent=intent,
            facts={
                "pay_date_a": _date("pay_date_a", date(2026, 4, 15)),
                "pay_amt_a": _money("pay_amt_a", 37_500),
                "pay_date_b": _date("pay_date_b", date(2026, 5, 15)),
                "pay_amt_b": _money("pay_amt_b", 37_500),
            },
            required={"pay_date_a", "pay_amt_a", "pay_date_b", "pay_amt_b"},
            next_phase=Phase.CONFIRM,
            template_override=(
                "On {pay_date_a} the creditor payment is {pay_amt_a}. "
                "On {pay_date_b} the creditor payment is {pay_amt_b}."
            ),
        )
    if intent == Intent.PROPOSE_WRAP:
        return Action(intent=intent, next_phase=Phase.WRAP)
    if intent == Intent.CLOSE:
        return Action(intent=intent, next_phase=Phase.END)
    if intent == Intent.NO_DEAL_WRAP:
        return Action(
            intent=intent,
            text_slots={
                "no_deal_reason": (
                    "No payment schedule fits within the client's program "
                    "under these terms."
                )
            },
            next_phase=Phase.END,
        )
    if intent == Intent.ESCALATE:
        return Action(
            intent=intent,
            text_slots={
                "escalate_reason": "This needs client approval for extra funds."
            },
            next_phase=Phase.ESCALATE,
        )
    if intent == Intent.ANSWER:
        return Action(
            intent=intent,
            text_slots={"answer_text": ANSWER_POINTS["next_steps"]},
            next_phase=Phase.NEGOTIATE,
        )
    raise AssertionError(intent)


@pytest.mark.parametrize("intent", list(Intent))
def test_every_intent_template_passes_guards(intent: Intent) -> None:
    assert intent in TEMPLATES
    action = _sample_action(intent)
    sentences = render_action(action, _REF)
    assert sentences
    assert sentences != [SAFE_FALLBACK]
    for s in sentences:
        assert s.strip()


def test_confirm_schedule_acks_settlement_pct() -> None:
    action = _sample_action(Intent.CONFIRM_SCHEDULE)
    sentences = render_action(action, _REF)
    assert sentences[0].startswith("45%")
    assert "works for us" in sentences[0].lower()


def test_bad_template_falls_back() -> None:
    action = Action(
        intent=Intent.COUNTER,
        facts={},  # missing required placeholders
        required={"counter_pct", "offer_total"},
        next_phase=Phase.NEGOTIATE,
    )
    assert render_action(action, _REF) == [SAFE_FALLBACK]


def test_counter_terms_min_payment_override_passes_guards() -> None:
    action = Action(
        intent=Intent.COUNTER_TERMS,
        facts={"alt_min_payment_cents": _money("alt_min_payment_cents", 5000)},
        required={"alt_min_payment_cents"},
        next_phase=Phase.NEGOTIATE,
        template_override=(
            "These terms do not fit the client's program at that minimum. "
            "Could you allow a lower minimum of {alt_min_payment_cents}?"
        ),
    )
    sentences = render_action(action, _REF)
    assert sentences != [SAFE_FALLBACK]
    joined = " ".join(sentences)
    assert "$50" in joined or "fifty" in joined.lower()


def test_counter_terms_max_payments_override_passes_guards() -> None:
    action = Action(
        intent=Intent.COUNTER_TERMS,
        facts={"alt_max_payments": _count("alt_max_payments", 8)},
        required={"alt_max_payments"},
        next_phase=Phase.NEGOTIATE,
        template_override=(
            "These terms do not fit the client's program at that payment count. "
            "Could you allow up to {alt_max_payments} payments?"
        ),
    )
    sentences = render_action(action, _REF)
    assert sentences != [SAFE_FALLBACK]
    joined = " ".join(sentences).lower()
    assert "eight" in joined or "8" in joined


def test_opening_is_time_neutral_and_agent_is_caller() -> None:
    """F13: no "Good morning" at any hour; the agent places the call."""
    lines = render_action(_sample_action(Intent.OPENING), _REF)
    text = " ".join(lines)
    assert text.startswith(
        "Hello, this is an automated agent calling on behalf of Synthetic Debt Relief"
    )
    assert "morning" not in text.lower()
    assert "thank you for calling" not in text.lower()
    assert "Let me check" not in text  # not the SAFE_FALLBACK
