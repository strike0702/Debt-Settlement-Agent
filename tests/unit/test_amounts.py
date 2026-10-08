"""Phase 39: dollar-total asks and the total-vs-per-payment clarify (no keys).

Covers the new NLU slots' quote verification, the code-side dollars → bp
conversion (round up), the triggers in ``resolve_amounts``, the deterministic
answer resolver, and the orchestrator path for the user's reported sentence
with a scripted (fake) NLU reply and with the oracle NLU.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.nlu import (
    VerifiedAnalysis,
    VerifiedTerm,
    bp_to_ask_pct,
    coerce_analysis_payload,
    post_verify,
    resolve_amounts,
    total_cents_to_bp,
    try_resolve_amount_clarify,
)
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.policy import (
    AMOUNT_CLARIFY_KEY,
    Intent,
    amount_meaning_clarify_action,
    ask_pct_to_bp,
)
from app.agent.reasons import REASON_SHORT, REASON_TEXT
from app.agent.session import CallSession
from app.config import Settings
from app.domain.belief import TermStatus
from app.domain.scenario import load_scenario
from app.llm.client import FakeLLM
from app.llm.prompts import nlu_messages
from app.store.audit import AuditLog

_REF = date(2026, 3, 1)
# fixtures/demo creditor_balance_cents.
_BALANCE = 125000
_USER_SENTENCE = (
    "You must pay $420 by March 31 to avoid further action. "
    "We only accept 3 even payments, so settle this now."
)


def _verify(analysis: TurnAnalysis, utterance: str) -> VerifiedAnalysis:
    return post_verify(analysis, utterance, ref=_REF)


# --- NLU verification of the new slots -------------------------------------


def test_total_slot_kept_when_quote_matches() -> None:
    out = _verify(
        TurnAnalysis(
            stance="offer", settlement_ask_total_cents=42000, ask_total_quote="$420 total"
        ),
        "We can settle this account for $420 total.",
    )
    assert out.settlement_ask_total_cents == 42000
    assert out.ask_total_quote == "$420 total"
    assert out.dropped == []


def test_total_slot_dropped_on_value_mismatch() -> None:
    out = _verify(
        TurnAnalysis(stance="offer", settlement_ask_total_cents=45000, ask_total_quote="$420"),
        "We can settle this account for $420 total.",
    )
    assert out.settlement_ask_total_cents is None
    assert [(d.field, d.reason) for d in out.dropped] == [
        ("settlement_ask_total_cents", "rejected_amount_value")
    ]


def test_total_slot_number_words() -> None:
    out = _verify(
        TurnAnalysis(
            stance="offer",
            settlement_ask_total_cents=120000,
            ask_total_quote="twelve hundred dollars",
        ),
        "To settle, we need twelve hundred dollars.",
    )
    assert out.settlement_ask_total_cents == 120000


def test_total_slot_dropped_when_quote_not_in_utterance() -> None:
    out = _verify(
        TurnAnalysis(stance="offer", settlement_ask_total_cents=42000, ask_total_quote="$420"),
        "We can settle this account soon.",
    )
    assert out.settlement_ask_total_cents is None
    assert [d.reason for d in out.dropped] == ["rejected_quote"]


def test_total_slot_rejects_bare_digits_without_unit() -> None:
    """A bare 420 could be dollars or cents: not accepted as a dollar total."""
    out = _verify(
        TurnAnalysis(stance="offer", settlement_ask_total_cents=42000, ask_total_quote="420"),
        "We could do 420 to close it.",
    )
    assert out.settlement_ask_total_cents is None


def test_ambiguous_slot_verified_like_total() -> None:
    ok = _verify(
        TurnAnalysis(stance="offer", amount_ambiguous_cents=42000, amount_ambiguous_quote="$420"),
        _USER_SENTENCE,
    )
    assert ok.amount_ambiguous_cents == 42000
    bad = _verify(
        TurnAnalysis(stance="offer", amount_ambiguous_cents=4200, amount_ambiguous_quote="$420"),
        _USER_SENTENCE,
    )
    assert bad.amount_ambiguous_cents is None


def test_coerce_nested_amount_shapes() -> None:
    out = coerce_analysis_payload(
        {
            "settlement_ask_total_cents": {"value": 42000, "quote": "$420"},
            "amount_ambiguous": {"cents": 9000, "quote": "$90"},
        }
    )
    assert out["settlement_ask_total_cents"] == 42000
    assert out["ask_total_quote"] == "$420"
    assert out["amount_ambiguous_cents"] == 9000
    assert out["amount_ambiguous_quote"] == "$90"
    assert "amount_ambiguous" not in out


def test_prompt_documents_new_slots() -> None:
    system = nlu_messages("x", "", None)[0]["content"]
    assert "settlement_ask_total_cents" in system
    assert "amount_ambiguous_cents" in system


# --- dollars → bp (code, Decimal, round up) ---------------------------------


def test_total_to_bp_exact() -> None:
    bp, exact = total_cents_to_bp(100000, _BALANCE)
    assert (bp, exact) == (8000, Decimal(8000))


def test_total_to_bp_rounds_up_never_down() -> None:
    bp, exact = total_cents_to_bp(42001, _BALANCE)
    assert exact == Decimal("3360.08")
    assert bp == 3361


@pytest.mark.parametrize("bp", [1, 3361, 4500, 4567, 9999, 10000])
def test_bp_pct_round_trip(bp: int) -> None:
    assert ask_pct_to_bp(bp_to_ask_pct(bp)) == bp


def test_resolve_total_becomes_ask(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.db")
    v = VerifiedAnalysis(
        stance="offer", settlement_ask_total_cents=42001, ask_total_quote="$420.01"
    )
    out, pending = resolve_amounts(
        v, "settle for $420.01", balance_cents=_BALANCE, audit=audit, call_id="c"
    )
    assert pending is None
    assert out.ask_verified and out.ask_total_bp == 3361
    assert ask_pct_to_bp(out.settlement_ask_pct or 0) == 3361
    assert out.ask_quote == "$420.01"
    rows = [r for r in audit.for_call("c") if r["type"] == "nlu_ask_total_to_bp"]
    assert rows and rows[0]["payload"]["exact_bp"] == "3360.08"
    assert rows[0]["payload"]["rounded_bp"] == 3361
    audit.close()


def test_resolve_pct_and_total_agree_within_rounding() -> None:
    """45% and $565 of $1,250 (45.2%) agree; the higher reading is the ask."""
    v = VerifiedAnalysis(
        stance="offer",
        settlement_ask_pct=45.0,
        ask_quote="45%",
        ask_verified=True,
        settlement_ask_total_cents=56500,
        ask_total_quote="$565",
    )
    out, pending = resolve_amounts(v, "45%, that is $565", balance_cents=_BALANCE)
    assert pending is None
    assert ask_pct_to_bp(out.settlement_ask_pct or 0) == 4520
    assert out.ask_quote == "45%"


def test_resolve_pct_and_total_disagree_is_ambiguous() -> None:
    v = VerifiedAnalysis(
        stance="offer",
        settlement_ask_pct=45.0,
        ask_quote="45%",
        ask_verified=True,
        settlement_ask_total_cents=90000,
        ask_total_quote="$900",
    )
    out, pending = resolve_amounts(v, "45%, so $900", balance_cents=_BALANCE)
    assert pending is not None and pending["trigger"] == "pct_total_disagree"
    assert pending["cents"] == 90000 and pending["pct"] == 45.0
    assert out.settlement_ask_pct is None and out.settlement_ask_total_cents is None


def test_resolve_total_above_balance_is_ambiguous() -> None:
    v = VerifiedAnalysis(
        stance="offer", settlement_ask_total_cents=200000, ask_total_quote="$2,000"
    )
    _, pending = resolve_amounts(v, "settle for $2,000", balance_cents=_BALANCE)
    assert pending is not None and pending["trigger"] == "total_exceeds_balance"


# --- plausibility / cue checks (question only, never a number change) -------


def _min_term(cents: int, quote: str) -> VerifiedTerm:
    return VerifiedTerm(field="min_payment_cents", value=cents, quote=quote, verified=True)


def _count_term(n: int, quote: str) -> VerifiedTerm:
    return VerifiedTerm(field="max_payments", value=n, quote=quote, verified=True)


def test_min_times_exact_count_above_balance_asks() -> None:
    utt = "We only accept 3 payments of $500."
    v = VerifiedAnalysis(stance="info", terms=[_min_term(50000, "$500"), _count_term(3, "3")])
    out, pending = resolve_amounts(v, utt, balance_cents=_BALANCE)
    assert pending is not None and pending["trigger"] == "min_exceeds_balance"
    # The doubtful amount is held back, not rewritten; the count still applies.
    assert [t.field for t in out.terms] == ["max_payments"]


def test_capped_count_does_not_multiply() -> None:
    """'up to 12 payments' is a cap: one payment is still allowed (seed-7 shape)."""
    utt = "We can take up to 12 payments, minimum $117, even payments."
    v = VerifiedAnalysis(stance="info", terms=[_min_term(11700, "$117"), _count_term(12, "12")])
    out, pending = resolve_amounts(v, utt, balance_cents=_BALANCE)
    assert pending is None and out.terms == v.terms


def test_total_cue_without_per_payment_cue_asks() -> None:
    utt = "You must pay $100 by March 31."
    v = VerifiedAnalysis(stance="info", terms=[_min_term(10000, "$100")])
    _, pending = resolve_amounts(v, utt, balance_cents=_BALANCE)
    assert pending is not None and pending["trigger"] == "total_cue"


def test_per_payment_cue_wins_over_total_cue() -> None:
    utt = "You must pay $100 by March 31, and that is the minimum each month."
    v = VerifiedAnalysis(stance="info", terms=[_min_term(10000, "$100")])
    _, pending = resolve_amounts(v, utt, balance_cents=_BALANCE)
    assert pending is None


def test_min_check_skipped_after_per_payment_answer() -> None:
    v = VerifiedAnalysis(stance="info", terms=[_min_term(200000, "$2,000")])
    _, pending = resolve_amounts(
        v, "per payment", balance_cents=_BALANCE, check_min_payment=False
    )
    assert pending is None


def test_nlu_flag_asks_and_holds_matching_min_term() -> None:
    v = VerifiedAnalysis(
        stance="offer",
        amount_ambiguous_cents=42000,
        amount_ambiguous_quote="$420",
        terms=[_min_term(42000, "$420"), _count_term(3, "3")],
    )
    out, pending = resolve_amounts(v, _USER_SENTENCE, balance_cents=_BALANCE)
    assert pending is not None and pending["trigger"] == "nlu_flag"
    assert [t.field for t in out.terms] == ["max_payments"]
    assert out.amount_ambiguous_cents is None


# --- answer resolution -------------------------------------------------------

_PENDING = {"cents": 42000, "quote": "$420", "trigger": "nlu_flag"}


@pytest.mark.parametrize(
    "reply",
    ["The total.", "That's the total settlement", "It's the total, not per payment.", "in full"],
)
def test_answer_total(reply: str) -> None:
    out = try_resolve_amount_clarify(reply, _PENDING, ref=_REF)
    assert out is not None and out.settlement_ask_total_cents == 42000
    assert out.terms == []


@pytest.mark.parametrize(
    "reply", ["Per payment.", "That's the minimum each month", "each payment, not the total."]
)
def test_answer_per_payment(reply: str) -> None:
    out = try_resolve_amount_clarify(reply, _PENDING, ref=_REF)
    assert out is not None and out.settlement_ask_total_cents is None
    assert [(t.field, t.value, t.verified) for t in out.terms] == [
        ("min_payment_cents", 42000, True)
    ]


def test_answer_restated_amount_with_cue() -> None:
    out = try_resolve_amount_clarify("Make it $450 total.", _PENDING, ref=_REF)
    assert out is not None and out.settlement_ask_total_cents == 45000


@pytest.mark.parametrize(
    "reply",
    ["$420", "yes", "hmm, let me check", "not per payment it is the total", "total per payment"],
)
def test_answer_unclear(reply: str) -> None:
    assert try_resolve_amount_clarify(reply, _PENDING, ref=_REF) is None


def test_answer_brings_back_held_pct() -> None:
    pending = {**_PENDING, "pct": 45.0, "pct_quote": "45%"}
    per = try_resolve_amount_clarify("per payment", pending, ref=_REF)
    assert per is not None and per.settlement_ask_pct == 45.0
    total = try_resolve_amount_clarify("the total", pending, ref=_REF)
    assert total is not None and total.settlement_ask_pct == 45.0
    # After a %-vs-total disagreement, "the total" overrules the %.
    overruled = try_resolve_amount_clarify(
        "the total", {**pending, "trigger": "pct_total_disagree"}, ref=_REF
    )
    assert overruled is not None and overruled.settlement_ask_pct is None


# --- action, reasons ---------------------------------------------------------


def test_clarify_action_is_template_only_with_public_fact() -> None:
    action = amount_meaning_clarify_action(cents=42000)
    assert action.intent == Intent.CLARIFY and action.reason == "amount_meaning"
    fact = action.facts["amount_in_question"]
    assert (fact.kind, fact.visibility, fact.source) == ("money", "PUBLIC", "creditor")
    assert action.template_override is not None
    assert "{amount_in_question}" in action.template_override
    assert [e.data for e in action.effects] == [{"field": AMOUNT_CLARIFY_KEY}]


def test_new_reason_codes_have_text() -> None:
    for key in ("amount_meaning", "amount_meaning_unresolved"):
        assert key in REASON_TEXT and key in REASON_SHORT


# --- orchestrator end to end -------------------------------------------------


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = dict(
        nlu_mode="oracle",
        nlg_mode="template",
        llm_profile="offline",
        max_turns=24,
        max_counters=4,
        firm_name="Synthetic Debt Relief",
        opening_disclosure="This call uses synthetic data for demonstration only.",
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def _orch(tmp_path: Path, *, llm: FakeLLM | None = None) -> tuple[Orchestrator, CallSession]:
    session = CallSession(scenario=load_scenario("fixtures/demo"))
    settings = _settings(nlu_mode="llm") if llm is not None else _settings()
    orch = Orchestrator(
        session,
        llm=llm,
        settings=settings,
        audit=AuditLog(tmp_path / "audit.db"),
        auto_ack=True,
    )
    await orch.start()
    return orch, session


def _nlu_reply(**fields: object) -> str:
    base: dict[str, object] = {"terms": [], "stance": "offer"}
    base.update(fields)
    return json.dumps(base)


# What a pre-Phase 39 NLU produced for the user's sentence: $420 as the minimum.
_OLD_STYLE_TERMS = [
    {"field": "min_payment_cents", "value": 42000, "quote": "$420", "hedged": False},
    {"field": "max_payments", "value": 3, "quote": "3 even payments", "hedged": False},
    {"field": "payment_structure", "value": "even", "quote": "even", "hedged": False},
]


@pytest.mark.asyncio
async def test_user_sentence_old_style_nlu_asks_instead_of_filing_minimum(
    tmp_path: Path,
) -> None:
    llm = FakeLLM()
    llm.enqueue("nlu", _nlu_reply(terms=_OLD_STYLE_TERMS))
    orch, session = await _orch(tmp_path, llm=llm)
    u = await orch.on_creditor_text(_USER_SENTENCE)
    assert u.action.intent == Intent.CLARIFY and u.action.reason == "amount_meaning"
    spoken = " ".join(text for _, text in u.sentences)
    assert "$420" in spoken and "total settlement" in spoken
    assert session.belief.get("min_payment_cents").status != TermStatus.KNOWN
    # The rest of the turn still lands.
    assert session.belief.get("max_payments").value == 3
    assert session.neg.pending_amount_clarify is not None

    # "The total." resolves without the LLM path mattering: ask = 420 / 1250 = 33.6%.
    llm.enqueue("nlu", _nlu_reply(stance="info"))
    u2 = await orch.on_creditor_text("The total.")
    assert session.neg.pending_amount_clarify is None
    assert session.neg.ask_bp == 3360
    assert session.belief.get("min_payment_cents").status != TermStatus.KNOWN
    assert u2.action.reason != "amount_meaning"


@pytest.mark.asyncio
async def test_user_sentence_ambiguous_flag_asks(tmp_path: Path) -> None:
    llm = FakeLLM()
    llm.enqueue(
        "nlu",
        _nlu_reply(
            terms=_OLD_STYLE_TERMS[1:],
            amount_ambiguous_cents=42000,
            amount_ambiguous_quote="$420",
        ),
    )
    orch, session = await _orch(tmp_path, llm=llm)
    u = await orch.on_creditor_text(_USER_SENTENCE)
    assert u.action.reason == "amount_meaning"

    llm.enqueue("nlu", _nlu_reply(stance="info"))
    await orch.on_creditor_text("That's the minimum per payment.")
    term = session.belief.get("min_payment_cents")
    assert (term.status, term.value) == (TermStatus.KNOWN, 42000)
    assert session.neg.ask_bp is None


@pytest.mark.asyncio
async def test_oracle_total_ask_reaches_engine_as_bp(tmp_path: Path) -> None:
    orch, session = await _orch(tmp_path)
    await orch.on_creditor_text(
        "Up to 6 payments, minimum $50, even, first payment March 15.",
        oracle=TurnAnalysis(
            stance="info",
            terms=[
                ExtractedTerm(field="max_payments", value=6, quote="6 payments"),
                ExtractedTerm(field="min_payment_cents", value=5000, quote="minimum $50"),
                ExtractedTerm(field="payment_structure", value="even", quote="even"),
                ExtractedTerm(
                    field="first_payment_date", value=date(2026, 3, 15), quote="March 15"
                ),
            ],
        ),
    )
    u = await orch.on_creditor_text(
        "We can settle for $1,000 total.",
        oracle=TurnAnalysis(
            stance="offer", settlement_ask_total_cents=100000, ask_total_quote="$1,000 total"
        ),
    )
    assert session.neg.ask_bp == 8000
    assert u.action.intent != Intent.CLARIFY
    assert u.trace is not None and u.trace.ask_bp == 8000


@pytest.mark.asyncio
async def test_unclear_answers_ask_twice_then_escalate(tmp_path: Path) -> None:
    orch, session = await _orch(tmp_path)
    amb = TurnAnalysis(stance="offer", amount_ambiguous_cents=60000, amount_ambiguous_quote="$600")
    u1 = await orch.on_creditor_text("We would need $600 from the client.", oracle=amb)
    assert u1.action.reason == "amount_meaning"
    u2 = await orch.on_creditor_text("Hmm.", oracle=TurnAnalysis(stance="other"))
    assert u2.action.reason == "amount_meaning"
    assert session.neg.clarify_counts[AMOUNT_CLARIFY_KEY] == 2
    u3 = await orch.on_creditor_text("Like I said.", oracle=TurnAnalysis(stance="other"))
    assert u3.action.intent == Intent.ESCALATE
    assert u3.action.reason == "amount_meaning_unresolved"
    assert session.neg.pending_amount_clarify is None


@pytest.mark.asyncio
async def test_closing_while_question_pending_is_not_asked_again(tmp_path: Path) -> None:
    orch, session = await _orch(tmp_path)
    amb = TurnAnalysis(stance="offer", amount_ambiguous_cents=60000, amount_ambiguous_quote="$600")
    await orch.on_creditor_text("We would need $600 from the client.", oracle=amb)
    u = await orch.on_creditor_text(
        "Thanks, goodbye.", oracle=TurnAnalysis(stance="other", wants_to_end=True)
    )
    assert u.action.reason != "amount_meaning"
    assert session.neg.pending_amount_clarify is None
