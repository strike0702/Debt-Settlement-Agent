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
