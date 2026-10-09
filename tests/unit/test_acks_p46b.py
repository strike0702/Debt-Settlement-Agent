"""Phase 46b: code-built acks, rep corrections, and the total-or-per-payment rule.

- Acks (``Settings.nlg_ack``, default on): built from ``ack_facts`` /
  ``ack_total_fact`` and ``ack_template`` only, spoken before the move, never
  before NO_ACK_INTENTS, never a private figure, wording rotated by turn.
- Corrections: "I said six payments" after an ack replaces the value (Phase 49:
  a bare "No, it's six" clarifies instead);
  "that's not what I said" reads the acked term back; a reply to a pending
  cents / amount question that carries new terms falls through to ``decide``.
- ``total_shape_trigger``: a verified dollar total in "pay $X by" or next to an
  exact count asks "total or per payment?" whatever the model returned.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.acts import (
    NO_ACK_INTENTS,
    ack_total_fact,
    acked_fields,
    attach_acts,
    is_ack_correction,
    is_ack_dispute,
)
from app.agent.guards import template_guard
from app.agent.nlg import ACK_TEMPLATES, TEMPLATES, ack_template, ack_variants, render_acts
from app.agent.nlu import (
    VerifiedAnalysis,
    VerifiedTerm,
    resolve_amounts,
    total_shape_trigger,
)
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.domain.actions import Action, Intent, Phase
from app.domain.belief import BeliefChange, TermStatus
from app.domain.scenario import load_scenario
from app.llm.client import FakeLLM
from app.store.audit import AuditLog
from tests.wsutil import offline_settings

_REF = date(2026, 3, 1)
_BALANCE = 125000  # fixtures/demo and easy_deal creditor balance
_AM09 = (
    "You must pay $420 by March 31 to avoid further action. "
    "We only accept 3 even payments, so settle this now."
)


def _orch(
    tmp_path: Path,
    *,
    llm: Any = None,
    fixture: str = "fixtures/scenarios/easy_deal",
    db: str = "a.db",
    **kw: Any,
) -> Orchestrator:
    if llm is not None:
        kw.setdefault("nlu_mode", "llm")
    session = CallSession(scenario=load_scenario(fixture))
    return Orchestrator(
        session,
        llm=llm,
        settings=offline_settings(**kw),
        audit=AuditLog(tmp_path / db),
        auto_ack=True,
    )


def _lines(utt: Any) -> list[str]:
    return [t for _, t in utt.sentences]


def _events(orch: Orchestrator, kind: str) -> list[dict[str, Any]]:
    assert orch.audit is not None
    return [e for e in orch.audit.for_call(orch.session.call_id) if e["type"] == kind]


def _terms(*pairs: tuple[str, Any, str]) -> list[ExtractedTerm]:
    return [ExtractedTerm(field=f, value=v, quote=q) for f, v, q in pairs]


# ----- ack wording -----


def test_every_ack_variant_passes_template_guard() -> None:
    shapes = [*ACK_TEMPLATES, ("ack_max_payments", "ack_total")]
    for ids in shapes:
        variants = ack_variants(ids)
        assert len(variants) >= 2, ids
        for t in variants:
            assert template_guard(t, set(ids), set(ids)).ok, t


def test_ack_wording_rotates_by_turn() -> None:
    ids = ("ack_max_payments",)
    picks = [ack_template(ids, turn) for turn in range(6)]
    assert all(p in ack_variants(ids) for p in picks)
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))
    assert len(set(picks)) >= 3


def test_h3_wording_unchanged() -> None:
    assert ACK_TEMPLATES[("ack_max_payments", "ack_min_payment")] == (
        "Got it, {ack_max_payments} payments at a {ack_min_payment} minimum."
    )
    assert ACK_TEMPLATES[("ack_total",)] == "Got it, {ack_total} in total."


# ----- ack facts -----


def _total_analysis(cents: int) -> VerifiedAnalysis:
    return VerifiedAnalysis(
        stance="offer",
        settlement_ask_total_cents=cents,
        ask_total_quote="$420",
        ask_total_bp=3360,
    )


def test_total_ack_only_for_a_total_that_became_the_ask() -> None:
    said = {("money", 42000)}
    fact = ack_total_fact(_total_analysis(42000), said, set())
    assert fact is not None and fact.visibility == "PUBLIC" and fact.source == "creditor"
    held = VerifiedAnalysis(stance="offer", settlement_ask_total_cents=42000)
    assert ack_total_fact(held, said, set()) is None  # not converted (question pending)
    assert ack_total_fact(_total_analysis(42000), set(), set()) is None  # rep never said it
    assert ack_total_fact(_total_analysis(42000), said, {("money", 42000)}) is None  # private


@pytest.mark.parametrize("intent", sorted(NO_ACK_INTENTS, key=lambda i: i.value))
def test_no_code_ack_before_no_ack_intents(intent: Intent) -> None:
    change = BeliefChange(
        field="max_payments",
        new_value=6,
        old_status=TermStatus.UNKNOWN,
        new_status=TermStatus.KNOWN,
        turn=1,
    )
    out = attach_acts(
        Action(intent=intent, next_phase=Phase.DISCOVERY),
        _total_analysis(42000),
        [change],
        creditor_numbers={("count", 6), ("money", 42000)},
        private_blocklist=set(),
        with_answer=False,
    )
    assert out.ack == {} and out.answer is None


def test_code_ack_attaches_no_answer_act() -> None:
    analysis = VerifiedAnalysis(stance="info", asks_question=True, question_topic="timeline")
    out = attach_acts(
        Action(intent=Intent.ASK_SETTLEMENT, next_phase=Phase.NEGOTIATE),
        analysis,
        [],
        creditor_numbers=set(),
        private_blocklist=set(),
        with_answer=False,
    )
    assert out.answer is None


def test_code_ack_renders_total_then_terms() -> None:
    action = attach_acts(
        Action(intent=Intent.COUNTER, next_phase=Phase.NEGOTIATE),
        _total_analysis(42000),
        [
            BeliefChange(
                field="max_payments",
                new_value=3,
                old_status=TermStatus.UNKNOWN,
                new_status=TermStatus.KNOWN,
                turn=2,
            )
        ],
        creditor_numbers={("count", 3), ("money", 42000)},
        private_blocklist=set(),
        with_answer=False,
    )
    assert list(action.ack) == ["ack_total", "ack_max_payments"]
    assert acked_fields(action.ack) == {"max_payments": 3}
    out = render_acts(
        action, _REF, creditor_numbers={("count", 3), ("money", 42000)}, turn=0, code_ack=True
    )
    assert out == ["Got it, $420 in total, up to 3 payments."]


# ----- orchestrator: ack on / off, order, privacy -----


_FIRST_TERMS = TurnAnalysis(
    stance="info",
    terms=_terms(
        ("max_payments", 8, "eight"),
        ("min_payment_cents", 10000, "one hundred dollars"),
        ("payment_structure", "even", "even"),
    ),
)
_FIRST_LINE = "Max eight payments, minimum one hundred dollars, even payments please."


@pytest.mark.asyncio
async def test_default_agent_acks_then_speaks_the_same_move(tmp_path: Path) -> None:
    on = _orch(tmp_path, db="on.db")
    off = _orch(tmp_path, db="off.db", nlg_ack=False)
    await on.start()
    await off.start()
    u_on = await on.on_creditor_text(_FIRST_LINE, oracle=_FIRST_TERMS)
    u_off = await off.on_creditor_text(_FIRST_LINE, oracle=_FIRST_TERMS)
    assert u_on.action.intent == u_off.action.intent == Intent.ASK_SETTLEMENT
    assert u_on.action.facts == u_off.action.facts
    lines_on, lines_off = _lines(u_on), _lines(u_off)
    assert lines_on[1:] == lines_off == [TEMPLATES[Intent.ASK_SETTLEMENT]]
    assert "8 payments" in lines_on[0] and "$100" in lines_on[0]
    assert lines_on[0].split(",")[0] in ("Got it", "Understood", "Okay")
    assert u_off.action.ack == {}
    # Code-built: no LLM, no answer act; audited with the move.
    assert _events(on, "decide")[-1]["payload"]["acts"] == {
        "ack": ["ack_max_payments", "ack_min_payment"],
        "answer": None,
    }


@pytest.mark.asyncio
async def test_private_figure_said_by_the_rep_is_never_acked(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    await orch.start()
    # $440 is on easy_deal's private blocklist.
    u = await orch.on_creditor_text(
        "Minimum is $440, up to 8 payments.",
        oracle=TurnAnalysis(
            stance="info",
            terms=_terms(("min_payment_cents", 44000, "$440"), ("max_payments", 8, "8")),
        ),
    )
    assert ("money", 44000) in orch.session.private_blocklist
    assert set(u.action.ack) <= {"ack_max_payments"}
    assert not any("$440" in line for line in _lines(u))


@pytest.mark.asyncio
async def test_verified_total_ask_is_acked(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    await orch.start()
    await orch.on_creditor_text(_FIRST_LINE, oracle=_FIRST_TERMS)
    u = await orch.on_creditor_text(
        "We can settle for $1,000 total.",
        oracle=TurnAnalysis(
            stance="offer", settlement_ask_total_cents=100000, ask_total_quote="$1,000 total"
        ),
    )
    assert orch.session.neg.ask_bp == 8000
    assert "ack_total" in u.action.ack
    first = _lines(u)[0]
    assert "$1,000" in first and ("in total" in first or "whole settlement" in first)


# ----- rep corrections -----


async def _acked_five(tmp_path: Path, **kw: Any) -> Orchestrator:
    orch = _orch(tmp_path, **kw)
    await orch.start()
    u = await orch.on_creditor_text(
        "Up to five payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 5, "five"))),
    )
    if kw.get("nlg_ack", True):
        assert "5 payments" in _lines(u)[0]
    return orch


@pytest.mark.asyncio
async def test_i_said_six_replaces_the_acked_value(tmp_path: Path) -> None:
    """Pair 18 shape: the ack echoed a misread; the rep's correction is applied.

    Phase 49: the line must say we misheard ("I said"); the P46b version of this
    test used "No, it's six payments.", which now clarifies (see below).
    """
    orch = await _acked_five(tmp_path)
    u = await orch.on_creditor_text(
        "No, I said six payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, "six"))),
    )
    term = orch.session.belief.get("max_payments")
    assert (term.status, term.value) == (TermStatus.KNOWN, 6)
    assert _events(orch, "ack_corrected")
    assert u.action.intent != Intent.CLARIFY
    assert "6 payments" in _lines(u)[0]  # the corrected value is acked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        "No, it's six payments.",
        # s0007_028 / s0007_034 (Phase 46c re-check): the contradictory persona's
        # change line as the sim rewrote it, and as the persona says it.
        "No, actually it's a maximum of 6 payments.",
        "No, actually make that a maximum of 6 payments.",
    ],
)
async def test_bare_no_with_a_new_value_clarifies(tmp_path: Path, line: str) -> None:
    """Phase 49 (ledger 46c.1): "no" + value may be the rep changing their rule."""
    orch = await _acked_five(tmp_path)
    quote = "6" if "6" in line else "six"
    u = await orch.on_creditor_text(
        line, oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, quote)))
    )
    assert u.action.intent == Intent.CLARIFY and u.action.reason == "max_payments"
    assert orch.session.belief.get("max_payments").status == TermStatus.CONTRADICTED
    assert not _events(orch, "ack_corrected")
    text = " ".join(_lines(u))
    assert "Earlier you mentioned 5" in text and "6" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    ["You misheard, six payments.", "You heard wrong, it's six payments."],
)
async def test_misreading_cues_replace_the_acked_value(tmp_path: Path, line: str) -> None:
    orch = await _acked_five(tmp_path)
    await orch.on_creditor_text(
        line, oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, "six")))
    )
    term = orch.session.belief.get("max_payments")
    assert (term.status, term.value) == (TermStatus.KNOWN, 6)
    assert _events(orch, "ack_corrected")


@pytest.mark.asyncio
async def test_rep_changing_their_own_term_still_clarifies(tmp_path: Path) -> None:
    orch = await _acked_five(tmp_path)
    u = await orch.on_creditor_text(
        "Actually the maximum is 7 payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 7, "7"))),
    )
    assert u.action.intent == Intent.CLARIFY and u.action.reason == "max_payments"
    assert orch.session.belief.get("max_payments").status == TermStatus.CONTRADICTED


@pytest.mark.asyncio
async def test_correction_without_an_ack_uses_the_clarify_path(tmp_path: Path) -> None:
    orch = await _acked_five(tmp_path, nlg_ack=False)
    u = await orch.on_creditor_text(
        "No, it's six payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, "six"))),
    )
    assert u.action.intent == Intent.CLARIFY
    assert not _events(orch, "ack_corrected")


@pytest.mark.asyncio
async def test_thats_not_what_i_said_reads_the_acked_term_back(tmp_path: Path) -> None:
    orch = await _acked_five(tmp_path)
    u = await orch.on_creditor_text(
        "That's not what I said.", oracle=TurnAnalysis(stance="other")
    )
    assert u.action.intent == Intent.READ_BACK and u.action.reason == "max_payments"
    assert u.action.ack == {}
    assert orch.session.belief.get("max_payments").status == TermStatus.TENTATIVE
    assert _events(orch, "ack_disputed")
    # The rep then gives the right number: it replaces the tentative value.
    await orch.on_creditor_text(
        "Six payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, "Six"))),
    )
    term = orch.session.belief.get("max_payments")
    assert (term.status, term.value) == (TermStatus.KNOWN, 6)


@pytest.mark.asyncio
async def test_live_nlu_correction_end_to_end(tmp_path: Path) -> None:
    llm = FakeLLM()
    reply = {"stance": "info", "terms": [
        {"field": "max_payments", "value": 5, "quote": "five", "hedged": False}
    ]}
    llm.enqueue("nlu", json.dumps(reply))
    orch = _orch(tmp_path, llm=llm)
    await orch.start()
    u1 = await orch.on_creditor_text("We can do up to five payments.")
    assert "5 payments" in _lines(u1)[0]
    reply["terms"][0].update(value=6, quote="six")
    llm.enqueue("nlu", json.dumps(reply))
    u2 = await orch.on_creditor_text("No, I said six payments, not five.")
    assert orch.session.belief.get("max_payments").value == 6
    assert u2.action.intent != Intent.CLARIFY


def test_correction_cues() -> None:
    # Phase 49: a bare "no" + value is no longer a correction (was True in P46b).
    assert not is_ack_correction("No, it's six payments.")
    assert not is_ack_correction("No, actually it's 6.")
    assert not is_ack_correction("No, actually make that 6.")
    assert is_ack_correction("I said six.")
    assert is_ack_correction("No, I said six.")
    assert is_ack_correction("You misheard.")
    assert is_ack_correction("You heard wrong.")
    assert is_ack_correction("That's not what I said, it's six.")
    assert not is_ack_correction("Actually, make that a maximum of 8 payments.")
    assert not is_ack_correction("Six payments, no problem.")
    assert is_ack_dispute("That's not what I said.")
    assert is_ack_dispute("You misheard me.")
    assert not is_ack_dispute("That's not right for us, the price is too low.")


# ----- pending questions answered with new terms -----


_AMB_600 = TurnAnalysis(
    stance="offer", amount_ambiguous_cents=60000, amount_ambiguous_quote="$600"
)


async def _pending_amount(tmp_path: Path) -> Orchestrator:
    orch = _orch(tmp_path, fixture="fixtures/demo")
    await orch.start()
    u = await orch.on_creditor_text("We would need $600 from the client.", oracle=_AMB_600)
    assert u.action.reason == "amount_meaning"
    return orch


@pytest.mark.asyncio
async def test_amount_question_unanswered_but_new_terms_falls_through(tmp_path: Path) -> None:
    orch = await _pending_amount(tmp_path)
    u = await orch.on_creditor_text(
        "We could do 40%, up to 6 payments.",
        oracle=TurnAnalysis(
            stance="counter",
            settlement_ask_pct=40.0,
            ask_quote="40%",
            terms=_terms(("max_payments", 6, "6 payments")),
        ),
    )
    assert u.action.reason != "amount_meaning"
    assert orch.session.neg.pending_amount_clarify is None
    assert orch.session.belief.get("max_payments").value == 6
    assert orch.session.neg.ask_bp == 4000
    dropped = _events(orch, "amount_clarify_dropped")
    assert dropped and dropped[-1]["payload"]["reason"] == "new_terms"


@pytest.mark.asyncio
async def test_amount_answer_plus_new_terms_applies_both(tmp_path: Path) -> None:
    orch = await _pending_amount(tmp_path)
    u = await orch.on_creditor_text(
        "That's the total, and up to 6 payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, "6 payments"))),
    )
    assert u.action.reason != "amount_meaning"
    assert orch.session.neg.ask_bp == 4800  # 600 / 1250
    assert orch.session.belief.get("max_payments").value == 6


@pytest.mark.asyncio
async def test_amount_answer_with_its_own_pct_ask_keeps_the_pct(tmp_path: Path) -> None:
    orch = await _pending_amount(tmp_path)
    u = await orch.on_creditor_text(
        "That's the total, but we could do 40%.",
        oracle=TurnAnalysis(stance="counter", settlement_ask_pct=40.0, ask_quote="40%"),
    )
    assert u.action.reason != "amount_meaning"
    assert orch.session.neg.ask_bp == 4000


@pytest.mark.asyncio
async def test_amount_question_unclear_reply_is_still_asked_again(tmp_path: Path) -> None:
    orch = await _pending_amount(tmp_path)
    u = await orch.on_creditor_text("Hmm.", oracle=TurnAnalysis(stance="other"))
    assert u.action.reason == "amount_meaning"


_CENTS_PENDING = {"field": "min_payment_cents", "bare": 110, "as_dollars": 11000, "as_cents": 110}


@pytest.mark.asyncio
async def test_cents_answer_plus_new_terms_applies_both(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    await orch.start()
    orch.session.neg.pending_cents_clarify = dict(_CENTS_PENDING)
    u = await orch.on_creditor_text(
        "Dollars, and up to 6 payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, "6 payments"))),
    )
    assert u.action.reason != "cents_ambiguity"
    assert orch.session.neg.pending_cents_clarify is None
    assert orch.session.belief.get("min_payment_cents").value == 11000
    assert orch.session.belief.get("max_payments").value == 6


@pytest.mark.asyncio
async def test_cents_unanswered_but_new_terms_falls_through(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    await orch.start()
    orch.session.neg.pending_cents_clarify = dict(_CENTS_PENDING)
    u = await orch.on_creditor_text(
        "Up to 6 payments.",
        oracle=TurnAnalysis(stance="info", terms=_terms(("max_payments", 6, "6 payments"))),
    )
    assert u.action.reason != "cents_ambiguity"
    assert orch.session.neg.pending_cents_clarify is None
    assert orch.session.belief.get("max_payments").value == 6
    assert _events(orch, "cents_clarify_dropped")


@pytest.mark.asyncio
async def test_cents_unclear_reply_is_still_asked_again(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    await orch.start()
    orch.session.neg.pending_cents_clarify = dict(_CENTS_PENDING)
    u = await orch.on_creditor_text("Hmm.", oracle=TurnAnalysis(stance="other"))
    assert u.action.reason == "cents_ambiguity"


# ----- total-or-per-payment rule (ledger 44.1) -----


def _total(cents: int, quote: str, **kw: Any) -> VerifiedAnalysis:
    return VerifiedAnalysis(
        stance="offer", settlement_ask_total_cents=cents, ask_total_quote=quote, **kw
    )


def _count(n: int, quote: str) -> VerifiedTerm:
    return VerifiedTerm(field="max_payments", value=n, quote=quote, verified=True)


def _min(cents: int, quote: str) -> VerifiedTerm:
    return VerifiedTerm(field="min_payment_cents", value=cents, quote=quote, verified=True)


@pytest.mark.parametrize(
    ("utterance", "cents", "trigger"),
    [
        (_AM09, 42000, "total_pay_by"),
        ("We would need $600 from the client in 3 even payments.", 60000, "total_with_count"),
        ("We need $600 from the client, three equal payments.", 60000, "total_with_count"),
        ("The client should pay $600 by April 30.", 60000, "total_pay_by"),
    ],
)
def test_total_in_per_payment_shape_asks(utterance: str, cents: int, trigger: str) -> None:
    """Groq / Cerebras read these as a total; code asks like Haiku does."""
    v = _total(cents, f"${cents // 100}", terms=[_count(3, "3")])
    out, pending = resolve_amounts(v, utterance, balance_cents=_BALANCE)
    assert pending is not None and pending["trigger"] == trigger
    assert pending["cents"] == cents
    assert out.settlement_ask_pct is None and out.ask_total_bp is None
    assert [t.field for t in out.terms] == ["max_payments"]  # the count still lands


@pytest.mark.parametrize(
    "utterance",
    [
        "We need $420 in total, in 3 even payments.",
        "That is $420 altogether, 3 even payments.",
        "We can settle for $420 in 3 payments.",
        # "We would need $600 from the client." (am10) was here; Phase 49 (ledger
        # 44b.5) asks about a bare demanded amount: tests/unit/test_nlu_rules_p49.py.
        "We take $600, up to 6 payments.",  # a cap, not an exact count
        "The client must pay $420 by March 31 in full.",
    ],
)
def test_explicit_or_plain_total_does_not_ask(utterance: str) -> None:
    cents = 60000 if "$600" in utterance else 42000
    v = _total(cents, f"${cents // 100}")
    out, pending = resolve_amounts(v, utterance, balance_cents=_BALANCE)
    assert pending is None and out.ask_total_bp is not None


@pytest.mark.parametrize(
    "utterance",
    [
        "The original balance of $6,000 is split into 3 even payments.",
        "The client owes $6,000, and we need 3 even payments.",
        "They must pay the outstanding $6,000 by March 31.",
    ],
)
def test_balance_statement_never_triggers_the_shape_question(utterance: str) -> None:
    """Cerebras n06 shape (Phase 44b): a statement of the debt is not an ask."""
    assert total_shape_trigger(600000, utterance) is None


def test_per_payment_amount_with_each_does_not_ask() -> None:
    utt = "We accept 3 payments of $140 each."
    v = VerifiedAnalysis(stance="info", terms=[_min(14000, "$140"), _count(3, "3")])
    out, pending = resolve_amounts(v, utt, balance_cents=_BALANCE)
    assert pending is None and out.terms == v.terms


def test_total_with_agreeing_pct_does_not_ask() -> None:
    v = _total(42000, "$420", settlement_ask_pct=33.6, ask_quote="33.6%", ask_verified=True)
    out, pending = resolve_amounts(
        v, "Pay $420 by March 31, that's 33.6%, in 3 payments.", balance_cents=_BALANCE
    )
    assert pending is None and out.ask_total_bp == 3360


@pytest.mark.asyncio
async def test_groq_style_total_reading_of_am09_asks_end_to_end(tmp_path: Path) -> None:
    llm = FakeLLM()
    llm.enqueue(
        "nlu",
        json.dumps(
            {
                "stance": "offer",
                "settlement_ask_total_cents": 42000,
                "ask_total_quote": "$420",
                "terms": [
                    {"field": "max_payments", "value": 3, "quote": "3 even payments",
                     "hedged": False},
                    {"field": "payment_structure", "value": "even", "quote": "even",
                     "hedged": False},
                ],
            }
        ),
    )
    orch = _orch(tmp_path, llm=llm, fixture="fixtures/demo")
    await orch.start()
    u = await orch.on_creditor_text(_AM09)
    assert u.action.intent == Intent.CLARIFY and u.action.reason == "amount_meaning"
    assert orch.session.neg.ask_bp is None
    assert orch.session.neg.pending_amount_clarify is not None
    assert orch.session.neg.pending_amount_clarify["trigger"] == "total_pay_by"
    assert orch.session.belief.get("max_payments").value == 3
