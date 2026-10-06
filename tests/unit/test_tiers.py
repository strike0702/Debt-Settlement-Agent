"""``min_payment_tiers`` end to end: ordinal render, NLU coerce, spoken facts, sim reveal.

Engine shape is ``[(from_payment_1based, min_cents), ...]``.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import pytest

from app.adapter.engine_adapter import Affordability
from app.agent.nlg import SAFE_FALLBACK, render_action
from app.agent.nlu import coerce_tiers, post_verify
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.policy import Intent, NegotiationState, decide
from app.config import Settings
from app.domain.belief import BeliefState
from app.domain.scenario import load_scenario
from app.domain.units import render_ordinal
from app.llm.prompts import nlu_messages
from app.store.audit import AuditLog

_REF = date(2026, 3, 1)
_SETTINGS = Settings(max_counters=4)
_AFFORD = Affordability(
    max_bp=10000, feasible_bps=list(range(100, 10001, 100)), curve=tuple([True] * 100)
)


@pytest.mark.parametrize(
    ("n", "out"),
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
     (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (101, "101st"), (111, "111th")],
)
def test_render_ordinal(n: int, out: str) -> None:
    assert render_ordinal(n) == out


# --- NLU ---------------------------------------------------------------------


def test_coerce_tiers_dicts_to_sorted_tuples() -> None:
    raw = [{"from_payment": 7, "min_cents": 10000}, {"from_payment": 4, "min_cents": 7500}]
    assert coerce_tiers(raw) == [(4, 7500), (7, 10000)]


def test_coerce_tiers_accepts_pairs_and_empty() -> None:
    assert coerce_tiers([(4, 7500)]) == [(4, 7500)]
    assert coerce_tiers([[4, 7500]]) == [(4, 7500)]
    assert coerce_tiers([]) == []


@pytest.mark.parametrize(
    "raw",
    [
        [{"up_to_payments": 3, "min_cents": 5000}],
        [{"from_payment": 0, "min_cents": 5000}],
        [{"from_payment": 4, "min_cents": -1}],
        [{"from_payment": 4, "min_cents": 5000}, {"from_payment": 4, "min_cents": 6000}],
        [{"from_payment": True, "min_cents": 5000}],
        [{"from_payment": "4", "min_cents": 5000}],
        [(4, 7500, 1)],
        {"from_payment": 4, "min_cents": 7500},
        "tiers",
    ],
)
def test_coerce_tiers_rejects_malformed(raw: object) -> None:
    assert coerce_tiers(raw) is None


def test_post_verify_coerces_tiers_and_keeps_unverified(tmp_path) -> None:
    utt = "From the fourth payment on, the minimum is $75."
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="min_payment_tiers",
                value=[{"from_payment": 4, "min_cents": 7500}],
                quote="From the fourth payment on, the minimum is $75",
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, utt, ref=_REF)
    assert [(t.field, t.value, t.verified) for t in out.terms] == [
        ("min_payment_tiers", [(4, 7500)], False)
    ]


def test_post_verify_drops_and_audits_malformed_tiers(tmp_path) -> None:
    audit = AuditLog(tmp_path / "a.db")
    utt = "Up to three payments the minimum is $50."
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="min_payment_tiers",
                value=[{"up_to_payments": 3, "min_cents": 5000}],
                quote="Up to three payments",
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, utt, ref=_REF, audit=audit, call_id="c1")
    assert out.terms == []
    events = [e["type"] for e in audit.for_call("c1")]
    assert "nlu_rejected_tiers" in events


def test_post_verify_first_n_payments_is_ambiguous(tmp_path) -> None:
    audit = AuditLog(tmp_path / "a.db")
    utt = "The minimum is $50 for the first three payments, then $75."
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(
                field="min_payment_tiers",
                value=[{"from_payment": 4, "min_cents": 7500}],
                quote="for the first three payments, then $75",
            )
        ],
        stance="info",
    )
    out = post_verify(analysis, utt, ref=_REF, audit=audit, call_id="c1")
    assert out.terms == []
    assert out.tiers_ambiguous is True
    assert out.to_turn_analysis().tiers_ambiguous is True
    assert "nlu_tiers_ambiguous" in [e["type"] for e in audit.for_call("c1")]


def test_first_payment_date_phrase_is_not_tier_ambiguity() -> None:
    utt = "The first payment is due March 31. There are no tiered minimums."
    analysis = TurnAnalysis(
        terms=[
            ExtractedTerm(field="min_payment_tiers", value=[], quote="no tiered minimums")
        ],
        stance="info",
    )
    out = post_verify(analysis, utt, ref=_REF)
    assert out.tiers_ambiguous is False
    assert out.terms[0].value == []


def test_nlu_prompt_uses_from_payment_schema() -> None:
    system = nlu_messages("x", "", None)[0]["content"]
    assert '"from_payment"' in system
    assert "up_to_payments" not in system


# --- policy speech -----------------------------------------------------------


def _belief_with_tiers(value: object) -> BeliefState:
    b = BeliefState(load_scenario("fixtures/demo").client)
    for f, v in dict(max_payments=8, min_payment_cents=5000, payment_structure="even").items():
        b.observe(f, v, "q", 1, verified=True, hedged=False)
    b.observe("min_payment_tiers", value, "tiers", 2, verified=False, hedged=False)
    return b


def _speak(action) -> str:
    return " ".join(render_action(action, _REF))


def test_readback_one_tier_speaks_ordinal_and_money() -> None:
    action = decide(
        _belief_with_tiers([(4, 7500)]),
        NegotiationState(turn_idx=2, ask_bp=4500),
        TurnAnalysis(stance="info"),
        _AFFORD,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.READ_BACK
    assert not action.text_slots.get("readback_value")
    assert {f.kind for f in action.facts.values()} == {"ordinal", "money"}
    assert all(f.visibility == "PUBLIC" for f in action.facts.values())
    text = _speak(action)
    assert text == "So I have a minimum of $75 from the 4th payment on. Is that right?"


def test_readback_two_tiers_joined() -> None:
    action = decide(
        _belief_with_tiers([(4, 7500), (7, 10000)]),
        NegotiationState(turn_idx=2, ask_bp=4500),
        TurnAnalysis(stance="info"),
        _AFFORD,
        settings=_SETTINGS,
    )
    text = _speak(action)
    assert text != SAFE_FALLBACK
    assert "a minimum of $75 from the 4th payment on" in text
    assert "a minimum of $100 from the 7th payment on" in text
    for ch in "[]{}()":
        assert ch not in text


def test_readback_empty_tiers() -> None:
    action = decide(
        _belief_with_tiers([]),
        NegotiationState(turn_idx=2, ask_bp=4500),
        TurnAnalysis(stance="info"),
        _AFFORD,
        settings=_SETTINGS,
    )
    assert _speak(action) == "So I have no special payment tiers. Is that right?"


def test_clarify_tiers_contradiction_speaks_both_values() -> None:
    b = _belief_with_tiers([(4, 7500)])
    b.confirm_readback("min_payment_tiers", True)
    b.observe("min_payment_tiers", [(5, 9000)], "tiers", 3, verified=False, hedged=False)
    action = decide(
        b,
        NegotiationState(turn_idx=3, ask_bp=4500),
        TurnAnalysis(stance="info"),
        _AFFORD,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CLARIFY
    text = _speak(action)
    assert "a minimum of $75 from the 4th payment on" in text
    assert "a minimum of $90 from the 5th payment on" in text


def test_tiers_ambiguous_clarifies_then_escalates() -> None:
    b = _belief_with_tiers([])
    action = decide(
        b,
        NegotiationState(turn_idx=3, ask_bp=4500),
        TurnAnalysis(stance="info", tiers_ambiguous=True),
        _AFFORD,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CLARIFY
    assert action.reason == "tiers_ambiguous"
    text = _speak(action)
    assert text != SAFE_FALLBACK
    assert "which payment" in text.lower()
    assert not any(ch.isdigit() for ch in text)

    action = decide(
        b,
        NegotiationState(turn_idx=4, ask_bp=4500, clarify_counts={"min_payment_tiers": 2}),
        TurnAnalysis(stance="info", tiers_ambiguous=True),
        _AFFORD,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE


# --- sim ---------------------------------------------------------------------


@lru_cache(maxsize=1)
def _tiered() -> tuple:
    from sim.scenarios import generate

    return tuple(s for s in generate(100, 7) if s.true_rules.min_payment_tiers)


def test_sim_generates_valid_nonempty_tiers() -> None:
    tiered = _tiered()
    assert tiered, "expected some scenarios with tiers"
    assert {len(s.true_rules.min_payment_tiers) for s in tiered} <= {1, 2}
    assert any(s.stratum == "deal" for s in tiered)
    for s in tiered:
        tr = s.true_rules
        froms = [f for f, _ in tr.min_payment_tiers]
        mins = [m for _, m in tr.min_payment_tiers]
        assert froms == sorted(set(froms))
        assert all(2 <= f <= tr.max_payments for f in froms)
        assert all(m > tr.min_payment_cents for m in mins)
        assert mins == sorted(mins)


def test_sim_reveals_tiers_in_words_with_tuple_oracle() -> None:
    from sim.creditor import CreditorPolicy

    sc = _tiered()[0]
    cp = CreditorPolicy(sc)
    text, terms = cp._reveal_field("min_payment_tiers")
    assert "payment on" in text
    for ch in "[](){}":
        assert ch not in text
    assert terms[0].value == list(sc.true_rules.min_payment_tiers)
    assert terms[0].quote in text


async def test_sim_readback_matches_tier_facts() -> None:
    from sim.creditor import CreditorPolicy

    sc = _tiered()[0]
    tiers = list(sc.true_rules.min_payment_tiers)
    b = _belief_with_tiers(tiers)
    action = decide(
        b, NegotiationState(turn_idx=2, ask_bp=4500), TurnAnalysis(stance="info"),
        _AFFORD, settings=_SETTINGS,
    )
    assert action.intent == Intent.READ_BACK
    reply = await CreditorPolicy(sc).respond(action)
    assert reply.analysis.readback_response == "confirm"

    wrong = [(f + 1, m) for f, m in tiers]
    action = decide(
        _belief_with_tiers(wrong), NegotiationState(turn_idx=2, ask_bp=4500),
        TurnAnalysis(stance="info"), _AFFORD, settings=_SETTINGS,
    )
    reply = await CreditorPolicy(sc).respond(action)
    assert reply.analysis.readback_response == "deny"
