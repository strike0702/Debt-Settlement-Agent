"""NLG deterministic templates pass both guards for every intent."""

from __future__ import annotations

from datetime import date

import pytest

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
                "num_payments": _count("num_payments", 6),
                "offer_total": _money("offer_total", 75_000),
                "first_payment_date": _date("first_payment_date", date(2026, 4, 15)),
            },
            required={"num_payments", "offer_total", "first_payment_date"},
            next_phase=Phase.CONFIRM,
        )
    if intent == Intent.PROPOSE_WRAP:
        return Action(intent=intent, next_phase=Phase.WRAP)
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


def test_bad_template_falls_back() -> None:
    action = Action(
        intent=Intent.COUNTER,
        facts={},  # missing required placeholders
        required={"counter_pct", "offer_total"},
        next_phase=Phase.NEGOTIATE,
    )
    assert render_action(action, _REF) == [SAFE_FALLBACK]
