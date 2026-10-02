"""Offline NLU / LLM-NLG tests using FakeLLM."""

from __future__ import annotations

from datetime import date

import pytest

from app.agent.nlg import SAFE_FALLBACK, render_action, speak_action
from app.agent.nlu import analyze, post_verify
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.policy import Action, Intent, Phase
from app.config import Settings
from app.domain.facts import Fact
from app.llm.client import FakeLLM

_REF = date(2026, 3, 15)


def _settings(**kwargs: object) -> Settings:
    base: dict[str, object] = {
        "nlu_mode": "llm",
        "nlg_mode": "llm",
        "llm_profile": "offline",
        "llm_cache": False,
    }
    base.update(kwargs)
    return Settings(**base)


@pytest.mark.asyncio
async def test_hallucinated_quote_dropped() -> None:
    fake = FakeLLM()
    fake.enqueue(
        "nlu",
        {
            "terms": [
                {
                    "field": "max_payments",
                    "value": 12,
                    "quote": "twelve payments maximum",
                    "hedged": False,
                }
            ],
            "stance": "info",
        },
    )
    out = await analyze(
        "We need even payments over six months.",
        "What are the payment terms?",
        None,
        llm=fake,
        settings=_settings(),
        ref=_REF,
    )
    assert out.terms == []


@pytest.mark.asyncio
async def test_about_two_fifty_verified_false() -> None:
    fake = FakeLLM()
    fake.enqueue(
        "nlu",
        {
            "terms": [
                {
                    "field": "min_payment_cents",
                    "value": 25000,
                    "quote": "about two-fifty",
                    "hedged": True,
                }
            ],
            "stance": "info",
        },
    )
    out = await analyze(
        "Minimum is about two-fifty a month.",
        "What's the minimum payment amount?",
        None,
        llm=fake,
        settings=_settings(),
        ref=_REF,
    )
    assert len(out.terms) == 1
    assert out.terms[0].value == 25000
    assert out.terms[0].verified is False
    assert out.terms[0].hedged is True


@pytest.mark.asyncio
async def test_dollar_250_becomes_25000_cents_verified() -> None:
    fake = FakeLLM()
    fake.enqueue(
        "nlu",
        {
            "terms": [
                {
                    "field": "min_payment_cents",
                    "value": 25000,
                    "quote": "$250",
                    "hedged": False,
                }
            ],
            "stance": "info",
        },
    )
    out = await analyze(
        "The floor is $250 per payment.",
        "What's the minimum payment amount?",
        None,
        llm=fake,
        settings=_settings(),
        ref=_REF,
    )
    assert len(out.terms) == 1
    assert out.terms[0].value == 25000
    assert out.terms[0].verified is True


@pytest.mark.asyncio
async def test_invalid_json_retried() -> None:
    fake = FakeLLM()
    fake.enqueue("nlu", "not-valid-json{{{")
    fake.enqueue(
        "nlu",
        {
            "terms": [
                {
                    "field": "max_payments",
                    "value": 6,
                    "quote": "six payments",
                    "hedged": False,
                }
            ],
            "stance": "info",
        },
    )
    out = await analyze(
        "We allow six payments max.",
        "What's the most payments they'll take?",
        None,
        llm=fake,
        settings=_settings(),
        ref=_REF,
    )
    assert len(fake.calls) == 2
    assert len(out.terms) == 1
    assert out.terms[0].field == "max_payments"
    assert out.terms[0].value == 6


@pytest.mark.asyncio
async def test_oracle_mode_skips_llm() -> None:
    fake = FakeLLM()
    oracle = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="payment_structure",
                value="even",
                quote="even",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = await analyze(
        "even payments only",
        "structure?",
        None,
        llm=fake,
        settings=_settings(nlu_mode="oracle"),
        oracle=oracle,
        ref=_REF,
    )
    assert fake.calls == []
    assert len(out.terms) == 1
    assert out.terms[0].verified is True


def test_post_verify_rejects_out_of_range() -> None:
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="max_payments",
                value=999,
                quote="999",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, "max is 999 payments", ref=_REF)
    assert out.terms == []


def test_post_verify_repairs_accept_stance() -> None:
    analysis = TurnAnalysis(stance="info")
    out = post_verify(
        analysis,
        "Yes, that payment schedule works for us. Agreed.",
        ref=_REF,
    )
    assert out.stance == "accept"


def test_post_verify_repairs_reject_stance() -> None:
    analysis = TurnAnalysis(stance="info")
    out = post_verify(
        analysis,
        "Those payment amounts are off — our minimum is actually $300.",
        ref=_REF,
    )
    assert out.stance == "reject"


def test_post_verify_repairs_wants_to_end_thanks() -> None:
    analysis = TurnAnalysis(stance="other", wants_to_end=False)
    out = post_verify(analysis, "thanks", ref=_REF)
    assert out.wants_to_end is True


def test_post_verify_clears_hostility_without_cues() -> None:
    """F03: LLM hostility alone must not escalate — need utterance cues."""
    analysis = TurnAnalysis(stance="other", hostility=0.95)
    out = post_verify(analysis, "six payments max please", ref=_REF)
    assert out.hostility == 0.0


def test_post_verify_keeps_hostility_with_cues() -> None:
    analysis = TurnAnalysis(stance="other", hostility=0.95)
    out = post_verify(analysis, "this is a waste of time you idiot", ref=_REF)
    assert out.hostility == 0.95


def test_post_verify_clears_private_flag_without_cues() -> None:
    """F03: false asks_client_private_info must not refuse/escalate."""
    analysis = TurnAnalysis(stance="other", asks_client_private_info=True)
    out = post_verify(analysis, "even payments only", ref=_REF)
    assert out.asks_client_private_info is False


def test_post_verify_keeps_private_flag_with_cues() -> None:
    analysis = TurnAnalysis(stance="other", asks_client_private_info=True)
    out = post_verify(
        analysis, "What is the client's bank balance?", ref=_REF
    )
    assert out.asks_client_private_info is True


def test_post_verify_clears_commitment_flag_without_cues() -> None:
    analysis = TurnAnalysis(stance="other", demands_commitment=True)
    out = post_verify(analysis, "can you send the schedule?", ref=_REF)
    assert out.demands_commitment is False


def test_post_verify_keeps_commitment_flag_with_cues() -> None:
    analysis = TurnAnalysis(stance="other", demands_commitment=True)
    out = post_verify(
        analysis, "I need a firm commitment that this deal is locked in.", ref=_REF
    )
    assert out.demands_commitment is True

def test_post_verify_repairs_asks_for_schedule() -> None:
    analysis = TurnAnalysis(stance="question", asks_for_schedule=False)
    out = post_verify(analysis, "I mean on a per date basis", ref=_REF)
    assert out.asks_for_schedule is True


def test_post_verify_revises_terms_instead() -> None:
    analysis = TurnAnalysis(stance="offer")
    out = post_verify(
        analysis, "can we do it in 3 payments instead", ref=_REF
    )
    assert out.revises_terms is True


def test_post_verify_firm_floor() -> None:
    analysis = TurnAnalysis(stance="info")
    out = post_verify(
        analysis, "65 is our floor, we cannot go lower", ref=_REF
    )
    assert out.firm is True
    assert out.stance == "reject"


def test_post_verify_not_firm_could_come_down() -> None:
    analysis = TurnAnalysis(stance="reject", firm=False)
    out = post_verify(
        analysis, "We could come down to 65", ref=_REF
    )
    assert out.firm is False


def test_post_verify_actually_not_revises_terms() -> None:
    analysis = TurnAnalysis(stance="info")
    out = post_verify(
        analysis, "Actually, make that a maximum of ten payments", ref=_REF
    )
    assert out.revises_terms is False


def test_six_not_verified_inside_sixteen() -> None:
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="max_payments",
                value=6,
                quote="six",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, "sixteen payments max", ref=_REF)
    assert out.terms == []


def test_250_not_verified_inside_1250() -> None:
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="min_payment_cents",
                value=25000,
                quote="250",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, "minimum is 1250 dollars", ref=_REF)
    assert out.terms == []


def test_post_verify_inflexible_not_flexible() -> None:
    """F01: value 'flexible' must not verify against quote 'inflexible'."""
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="payment_structure",
                value="flexible",
                quote="inflexible",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, "we are inflexible on structure", ref=_REF)
    assert len(out.terms) == 1
    assert out.terms[0].value == "flexible"
    assert out.terms[0].verified is False


def test_post_verify_evening_quote_not_even() -> None:
    """F01: value 'even' must not verify against quote 'evening'."""
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="payment_structure",
                value="even",
                quote="evening",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, "call me this evening", ref=_REF)
    assert len(out.terms) == 1
    assert out.terms[0].value == "even"
    assert out.terms[0].verified is False


def test_ask_without_quote_cleared() -> None:
    analysis = TurnAnalysis(
        settlement_ask_pct=95.0,
        ask_quote=None,
        stance="info",
    )
    out = post_verify(analysis, "we want a good deal", ref=_REF)
    assert out.settlement_ask_pct is None
    assert out.ask_verified is False


def test_ask_mismatched_value_cleared() -> None:
    analysis = TurnAnalysis(
        settlement_ask_pct=40.0,
        ask_quote="ninety-five percent",
        stance="info",
    )
    out = post_verify(
        analysis, "we need ninety-five percent settlement", ref=_REF
    )
    assert out.settlement_ask_pct is None
    assert out.ask_verified is False


def test_ask_matches_uses_ask_pct_to_bp_half_up() -> None:
    """F02: verify ask bp with ask_pct_to_bp (HALF_UP), not float round."""
    from app.agent.policy import ask_pct_to_bp

    assert ask_pct_to_bp(45.125) == 4513
    assert int(round(45.125 * 100)) == 4512

    # Quote token 4513 must match policy bp (fails under int(round)=4512).
    analysis = TurnAnalysis(
        settlement_ask_pct=45.125,
        ask_quote="45.13 percent",
        stance="info",
    )
    out = post_verify(analysis, "we need 45.13 percent settlement", ref=_REF)
    assert out.settlement_ask_pct == 45.125
    assert out.ask_verified is True

    # Quote token 4512 must NOT match policy bp 4513.
    bad = TurnAnalysis(
        settlement_ask_pct=45.125,
        ask_quote="45.12 percent",
        stance="info",
    )
    out_bad = post_verify(bad, "we need 45.12 percent settlement", ref=_REF)
    assert out_bad.settlement_ask_pct is None
    assert out_bad.ask_verified is False


def test_ask_verified_when_quote_matches() -> None:
    analysis = TurnAnalysis(
        settlement_ask_pct=45.0,
        ask_quote="forty five percent",
        stance="counter",
    )
    out = post_verify(
        analysis, "We are looking for a forty five percent settlement.", ref=_REF
    )
    assert out.settlement_ask_pct == 45.0
    assert out.ask_verified is True


def test_spoofed_readback_response_nullified() -> None:
    analysis = TurnAnalysis(stance="info", readback_response="confirm")
    out = post_verify(analysis, "please hold", ref=_REF)
    assert out.readback_response is None


def test_readback_confirm_phrase_kept() -> None:
    analysis = TurnAnalysis(stance="info", readback_response="confirm")
    out = post_verify(analysis, "Yes, that is correct.", ref=_REF)
    assert out.readback_response == "confirm"


def test_tiers_never_auto_verified() -> None:
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="min_payment_tiers",
                value=[{"up_to_payments": 99, "min_cents": 1}],
                quote="tiers",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, "we have special tiers", ref=_REF)
    assert len(out.terms) == 1
    assert out.terms[0].verified is False


@pytest.mark.asyncio
async def test_nlg_bad_template_falls_back_to_deterministic() -> None:
    fake = FakeLLM()
    # Digit → template_guard fail
    fake.enqueue("nlg", "We can propose 40 percent of the balance.")
    # Unknown placeholder → fail again
    fake.enqueue("nlg", "We can propose {mystery_pct} of the balance.")

    action = Action(
        intent=Intent.COUNTER,
        facts={
            "counter_pct": Fact(
                id="counter_pct",
                kind="pct",
                value=4000,
                visibility="PUBLIC",
                source="engine",
            ),
            "offer_total": Fact(
                id="offer_total",
                kind="money",
                value=50_000,
                visibility="PUBLIC",
                source="engine",
            ),
        },
        required={"counter_pct", "offer_total"},
        next_phase=Phase.NEGOTIATE,
    )
    lines = await speak_action(
        action,
        _REF,
        llm=fake,
        settings=_settings(nlg_mode="llm"),
        last_rep_line="Can you do forty percent?",
    )
    assert len(fake.calls) == 2
    assert lines == render_action(action, _REF)
    assert SAFE_FALLBACK not in lines


@pytest.mark.asyncio
async def test_nlg_good_llm_template_used() -> None:
    fake = FakeLLM()
    fake.enqueue(
        "nlg",
        "Would {counter_pct} totaling {offer_total} work for your desk?",
    )
    action = Action(
        intent=Intent.COUNTER,
        facts={
            "counter_pct": Fact(
                id="counter_pct",
                kind="pct",
                value=4000,
                visibility="PUBLIC",
                source="engine",
            ),
            "offer_total": Fact(
                id="offer_total",
                kind="money",
                value=50_000,
                visibility="PUBLIC",
                source="engine",
            ),
        },
        required={"counter_pct", "offer_total"},
        next_phase=Phase.NEGOTIATE,
    )
    lines = await speak_action(
        action,
        _REF,
        llm=fake,
        settings=_settings(nlg_mode="llm"),
    )
    assert len(fake.calls) == 1
    assert lines != render_action(action, _REF)
    joined = " ".join(lines)
    assert "Would" in joined
    assert "40%" in joined
    assert "$500" in joined
    assert SAFE_FALLBACK not in lines


@pytest.mark.asyncio
async def test_nlg_ask_skips_llm_and_speaks_ask_text() -> None:
    fake = FakeLLM()
    fake.enqueue("nlg", "Could you please provide the max_payments for {ask_text}?")
    ask = "What is the maximum number of payments you can accept?"
    action = Action(
        intent=Intent.ASK,
        text_slots={"ask_text": ask, "field_label": "maximum number of payments"},
        required={"ask_text"},
        next_phase=Phase.DISCOVERY,
    )
    lines = await speak_action(action, _REF, llm=fake, settings=_settings(nlg_mode="llm"))
    assert fake.calls == []
    assert lines == [ask]


@pytest.mark.asyncio
async def test_nlg_no_deal_wrap_skips_llm() -> None:
    fake = FakeLLM()
    fake.enqueue("nlg", "I'm sorry, but we cannot proceed due to {no_deal_reason}.")
    action = Action(
        intent=Intent.NO_DEAL_WRAP,
        text_slots={"no_deal_reason": "Understood, we will end the call here."},
        next_phase=Phase.END,
    )
    lines = await speak_action(action, _REF, llm=fake, settings=_settings(nlg_mode="llm"))
    assert fake.calls == []
    assert lines == ["Understood, we will end the call here.", "Thank you for your time."]


def test_post_verify_rejects_bare_year_date() -> None:
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="first_payment_date",
                value="2026-01-01",
                quote="2026",
                hedged=False,
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, "i mean 2026", ref=_REF)
    assert out.terms == []


@pytest.mark.asyncio
async def test_fast_readback_skips_llm() -> None:
    from app.agent.nlu import analyze
    from app.config import Settings
    from app.llm.client import FakeLLM

    llm = FakeLLM()
    # If LLM were called, chat_text would raise (empty queue).
    settings = Settings(nlu_mode="llm", llm_profile="offline", nlg_mode="template")
    out = await analyze(
        "yes",
        "So I have October 31 for the initial payment date. Is that right?",
        "first_payment_date",
        llm=llm,
        settings=settings,
    )
    assert out.readback_response == "confirm"
