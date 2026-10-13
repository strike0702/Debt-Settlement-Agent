"""The correction loop from a live call (audit 9a1e7d40), and the fixes for it.

- Change requests ("can you change it to $250", "make that 6") on a value we
  just acknowledged update it directly; a bare "No, it's $250" still clarifies.
- A short reply that names one side of an open clarify ("$250", "the first
  one") settles it in code, with no LLM call and no total-or-per-payment
  question.
- A clarify / ask the rep heard part of before barging in still counts, so the
  second unresolved clarify hands off.
- The NLU prompt has no turn context: it was measured (Phase 54) and dropped,
  so the live call sends the same prompt as before. A value the model lifts
  from an earlier turn still fails the verbatim-quote check.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.acts import is_ack_correction, is_change_request
from app.agent.nlu import try_resolve_disputed_value
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.domain.actions import Intent
from app.domain.belief import TermStatus
from app.domain.fields import FIELDS_BY_NAME
from app.domain.scenario import load_scenario
from app.llm.client import FakeLLM
from app.llm.prompts import nlu_messages
from app.store.audit import AuditLog
from tests.wsutil import offline_settings

_REF = date(2026, 4, 1)


class _RecordingLLM(FakeLLM):
    """FakeLLM that also keeps the messages of each NLU call."""

    def __init__(self) -> None:
        super().__init__()
        self.nlu_messages: list[list[dict[str, Any]]] = []

    async def chat_text(self, role, messages, max_tokens, *, json_mode=False):  # type: ignore[no-untyped-def]
        if role == "nlu":
            self.nlu_messages.append([dict(m) for m in messages])
        return await super().chat_text(role, messages, max_tokens, json_mode=json_mode)


def _nlu(*terms: tuple[str, Any, str], stance: str = "info") -> str:
    return json.dumps(
        {
            "terms": [{"field": f, "value": v, "quote": q, "hedged": False} for f, v, q in terms],
            "stance": stance,
        }
    )


def _orch(tmp_path: Path, *, llm: Any = None, auto_ack: bool = True) -> Orchestrator:
    kw: dict[str, Any] = {"nlu_mode": "llm"} if llm is not None else {}
    return Orchestrator(
        CallSession(scenario=load_scenario("fixtures/scenarios/easy_deal")),
        llm=llm,
        settings=offline_settings(**kw),
        audit=AuditLog(tmp_path / "a.db"),
        auto_ack=auto_ack,
    )


def _lines(utt: Any) -> list[str]:
    return [t for _, t in utt.sentences]


def _events(orch: Orchestrator, kind: str) -> list[dict[str, Any]]:
    assert orch.audit is not None
    return [e for e in orch.audit.for_call(orch.session.call_id) if e["type"] == kind]


async def _min_180_acked(tmp_path: Path) -> tuple[Orchestrator, _RecordingLLM]:
    """Call up to "Got it, at least $180 per payment. Do you need even payments …?"."""
    llm = _RecordingLLM()
    orch = _orch(tmp_path, llm=llm)
    await orch.start()
    llm.enqueue("nlu", _nlu(("max_payments", 8, "eight")))
    u = await orch.on_creditor_text("Well, it is eight.")
    assert u.action.reason == "min_payment_cents"
    u = await orch.on_creditor_text("$180")  # fast path: bare answer to our ask
    assert "ack_min_payment" in u.action.ack and "$180" in _lines(u)[0]
    assert u.action.reason == "payment_structure"
    assert orch.session.belief.get("min_payment_cents").status == TermStatus.KNOWN
    return orch, llm


# ----- A. change requests -----


@pytest.mark.parametrize(
    "line",
    [
        "Sorry, can you change it to $250?",
        "Change that to 6.",
        "Make it $250.",
        "Make that 6.",
        "Let's change that to eight.",
        "Could you update it to $300?",
        "Switch it to 7, please.",
        "No, actually make that 6.",
        "Can we make it the 15th instead?",
    ],
)
def test_change_request_cues(line: str) -> None:
    assert is_change_request(line)


@pytest.mark.parametrize(
    "line",
    [
        "No, it's 6.",
        "No, actually it's 6.",
        "We can't change it to 6.",
        "Can't change it to 6.",
        "We won't change that to $250.",
        "Should we change it to 50%?",
        "Why did you change it to 6?",
        "Do you want to change it to 6?",
        "If we change it to 6, does that work?",
        "Can you make that work?",
        "Make it quick, please.",
    ],
)
def test_not_change_request_cues(line: str) -> None:
    assert not is_change_request(line)


def test_change_request_is_not_a_misheard_cue() -> None:
    # Two separate cues; the orchestrator accepts either.
    assert not is_ack_correction("Can you change it to $250?")


@pytest.mark.asyncio
async def test_live_call_change_request_updates_the_acked_minimum(tmp_path: Path) -> None:
    """Regression (audit 9a1e7d40): the change request is acked, not clarified."""
    orch, llm = await _min_180_acked(tmp_path)
    llm.enqueue("nlu", _nlu(("min_payment_cents", 25000, "$250"), stance="counter"))
    u = await orch.on_creditor_text("Sorry, can you change it to $250?")
    assert u.action.intent != Intent.CLARIFY
    # The ack of the new value ("Got it, at least $250 per payment." on the live
    # call's turn; the opener and body rotate by turn).
    assert "ack_min_payment" in u.action.ack and "$250" in _lines(u)[0]
    term = orch.session.belief.get("min_payment_cents")
    assert (term.status, term.value) == (TermStatus.KNOWN, 25000)
    (ev,) = _events(orch, "ack_corrected")
    assert ev["payload"]["cue"] == "change_request"
    assert ev["payload"]["old_value"] == 18000


@pytest.mark.asyncio
async def test_change_request_with_nothing_acked_still_clarifies(tmp_path: Path) -> None:
    orch, llm = await _min_180_acked(tmp_path)
    orch._last_ack = {}  # the previous line acked nothing
    llm.enqueue("nlu", _nlu(("min_payment_cents", 25000, "$250"), stance="counter"))
    u = await orch.on_creditor_text("Can you change it to $250?")
    assert u.action.intent == Intent.CLARIFY


@pytest.mark.asyncio
async def test_rules_restated_with_make_that_still_clarifies(tmp_path: Path) -> None:
    """The contradictory persona's flip restates several terms: CONTRADICTED → CLARIFY."""
    orch = _orch(tmp_path)
    await orch.start()
    await orch.on_creditor_text(
        "Up to five payments.",
        oracle=TurnAnalysis(
            stance="info", terms=[ExtractedTerm(field="max_payments", value=5, quote="five")]
        ),
    )
    u = await orch.on_creditor_text(
        "Actually, make that a maximum of 7 payments, minimum $100, flexible structure.",
        oracle=TurnAnalysis(
            stance="info",
            terms=[
                ExtractedTerm(field="max_payments", value=7, quote="7"),
                ExtractedTerm(field="min_payment_cents", value=10000, quote="$100"),
                ExtractedTerm(field="payment_structure", value="flexible", quote="flexible"),
            ],
        ),
    )
    assert u.action.intent == Intent.CLARIFY and u.action.reason == "max_payments"
    assert not _events(orch, "ack_corrected")


# ----- B. disputed-value answers -----


@pytest.mark.parametrize(
    ("reply", "want"),
    [
        ("$250", 25000),
        ("$180.", 18000),
        ("No, $250.", 25000),
        ("250 dollars", 25000),
        ("$250 please", 25000),
        ("the first one", 18000),
        ("The earlier one.", 18000),
        ("the new one", 25000),
        ("The latter.", 25000),
        ("250", None),  # no unit: the dollars-or-cents path decides
        ("$300", None),  # a third value is a new value
        ("$180 or $250", None),
        ("not $250", None),
        ("maybe $250", None),
        ("it's the minimum for each payment", None),
        ("We need two hundred fifty dollars a month.", None),  # over the size limits
    ],
)
def test_disputed_cents_reply(reply: str, want: int | None) -> None:
    got = try_resolve_disputed_value(reply, "min_payment_cents", 18000, 25000, ref=_REF)
    assert (got.terms[0].value if got else None) == want
    if got:
        assert got.terms[0].verified and not got.terms[0].hedged


@pytest.mark.parametrize(
    ("reply", "want"),
    [("6", 6), ("eight", 8), ("8 payments", 8), ("the second one", 8), ("ten", None)],
)
def test_disputed_count_reply(reply: str, want: int | None) -> None:
    got = try_resolve_disputed_value(reply, "max_payments", 6, 8, ref=_REF)
    assert (got.terms[0].value if got else None) == want


@pytest.mark.parametrize(
    ("reply", "want"),
    [
        ("March 31", date(2026, 3, 31)),
        ("April 15.", date(2026, 4, 15)),
        ("the first one", date(2026, 3, 31)),
        ("the first", None),  # could be the 1st of the month
        ("May 2", None),
    ],
)
def test_disputed_date_reply(reply: str, want: date | None) -> None:
    got = try_resolve_disputed_value(
        reply, "first_payment_date", date(2026, 3, 31), date(2026, 4, 15), ref=_REF
    )
    assert (got.terms[0].value if got else None) == want


@pytest.mark.asyncio
async def test_bare_no_clarifies_then_bare_value_settles_it(tmp_path: Path) -> None:
    """"No, it's $250" → CLARIFY → "$250" → KNOWN $250, no LLM call, no amount question."""
    orch, llm = await _min_180_acked(tmp_path)
    llm.enqueue("nlu", _nlu(("min_payment_cents", 25000, "$250")))
    u = await orch.on_creditor_text("No, it's $250.")
    assert u.action.intent == Intent.CLARIFY and u.action.reason == "min_payment_cents"
    calls = len(llm.calls)
    # The FakeLLM queue is empty: an NLU call here would raise LLMUnavailable.
    u = await orch.on_creditor_text("$250")
    assert len(llm.calls) == calls
    term = orch.session.belief.get("min_payment_cents")
    assert (term.status, term.value) == (TermStatus.KNOWN, 25000)
    assert u.action.intent != Intent.CLARIFY
    assert orch.session.neg.pending_amount_clarify is None
    assert not _events(orch, "nlu_amount_ambiguous")
    (ev,) = _events(orch, "clarify_resolved")
    assert ev["payload"] == {
        "field": "min_payment_cents",
        "value": 25000,
        "old": 18000,
        "new": 25000,
        "quote": "$250",
    }
    assert "ack_min_payment" in u.action.ack and "$250" in _lines(u)[0]


@pytest.mark.asyncio
async def test_pick_by_order_keeps_the_old_value(tmp_path: Path) -> None:
    orch, llm = await _min_180_acked(tmp_path)
    llm.enqueue("nlu", _nlu(("min_payment_cents", 25000, "$250")))
    await orch.on_creditor_text("No, it's $250.")
    await orch.on_creditor_text("The first one.")
    term = orch.session.belief.get("min_payment_cents")
    assert (term.status, term.value) == (TermStatus.KNOWN, 18000)


@pytest.mark.asyncio
async def test_pick_overrides_an_empty_or_ambiguous_reading(tmp_path: Path) -> None:
    """Oracle / model reading "$250" as nothing, or as an ambiguous amount: code settles it."""
    orch = _orch(tmp_path)
    await orch.start()
    for text, value in (("Minimum $180.", 18000), ("No, it's $250.", 25000)):
        await orch.on_creditor_text(
            text,
            oracle=TurnAnalysis(
                stance="info",
                terms=[ExtractedTerm(field="min_payment_cents", value=value, quote=text[-5:-1])],
            ),
        )
    assert orch.session.belief.get("min_payment_cents").status == TermStatus.CONTRADICTED
    u = await orch.on_creditor_text(
        "$250", oracle=TurnAnalysis(stance="info", amount_ambiguous_cents=25000,
                                   amount_ambiguous_quote="$250")
    )
    term = orch.session.belief.get("min_payment_cents")
    assert (term.status, term.value) == (TermStatus.KNOWN, 25000)
    assert u.action.reason != "amount_meaning"
    assert orch.session.neg.pending_amount_clarify is None


@pytest.mark.asyncio
async def test_third_value_is_a_new_value(tmp_path: Path) -> None:
    orch, llm = await _min_180_acked(tmp_path)
    llm.enqueue("nlu", _nlu(("min_payment_cents", 25000, "$250")))
    await orch.on_creditor_text("No, it's $250.")
    llm.enqueue("nlu", _nlu(("min_payment_cents", 30000, "$300")))
    await orch.on_creditor_text("$300")
    assert not _events(orch, "clarify_resolved")
    assert orch.session.belief.get("min_payment_cents").value == 30000


# ----- C. barged questions count -----


async def _say(orch: Orchestrator, text: str, oracle: TurnAnalysis, *, barge: bool) -> Any:
    """One rep turn with manual acks: barge after the first sentence, or hear it all."""
    u = await orch.on_creditor_text(text, oracle=oracle)
    ids = [sid for sid, _ in u.sentences]
    if barge:
        await orch.on_barge_in(ids[:1])
    else:
        await orch.on_sentence_done(ids)
    return u


@pytest.mark.asyncio
async def test_barged_clarify_counts_and_second_unresolved_hands_off(tmp_path: Path) -> None:
    orch = _orch(tmp_path, auto_ack=False)
    u = await orch.start()
    await orch.on_sentence_done([sid for sid, _ in u.sentences])

    def mx(v: int, q: str) -> TurnAnalysis:
        return TurnAnalysis(
            stance="info", terms=[ExtractedTerm(field="max_payments", value=v, quote=q)]
        )

    await _say(orch, "Up to six payments.", mx(6, "six"), barge=False)
    u = await _say(orch, "No, it's eight payments.", mx(8, "eight"), barge=True)
    assert u.action.intent == Intent.CLARIFY
    assert orch.session.neg.clarify_counts["max_payments"] == 1
    assert orch.session.neg.question_counts["CLARIFY:max_payments"] == 1
    u = await _say(orch, "Hmm, hold on.", TurnAnalysis(stance="stall"), barge=True)
    assert u.action.intent == Intent.CLARIFY
    assert orch.session.neg.clarify_counts["max_payments"] == 2
    u = await _say(orch, "Hmm.", TurnAnalysis(stance="other"), barge=False)
    assert u.action.intent == Intent.ESCALATE
    assert u.action.reason == "contradiction_unresolved"
    barges = _events(orch, "barge_in")
    assert [e["payload"]["kept_effects"][0]["kind"] for e in barges] == [
        "note_clarify",
        "note_clarify",
    ]


@pytest.mark.asyncio
async def test_unheard_question_counts_for_nothing(tmp_path: Path) -> None:
    orch = _orch(tmp_path, auto_ack=False)
    u = await orch.start()
    await orch.on_sentence_done([sid for sid, _ in u.sentences])
    u = await orch.on_creditor_text("Hello.", oracle=TurnAnalysis(stance="other"))
    assert u.action.intent == Intent.ASK
    await orch.on_barge_in([])
    assert orch.session.neg.question_counts == {}


@pytest.mark.asyncio
async def test_heard_ask_keeps_only_its_question_count(tmp_path: Path) -> None:
    orch = _orch(tmp_path, auto_ack=False)
    u = await orch.start()
    await orch.on_sentence_done([sid for sid, _ in u.sentences])
    u = await orch.on_creditor_text("Hello.", oracle=TurnAnalysis(stance="other"))
    assert u.action.intent == Intent.ASK
    phase = orch.session.neg.phase
    await orch.on_barge_in([u.sentences[0][0]])
    assert orch.session.neg.question_counts == {f"ASK:{u.action.reason}": 1}
    assert orch.session.neg.phase == phase
    (ev,) = _events(orch, "barge_in")
    assert [e["kind"] for e in ev["payload"]["kept_effects"]] == ["note_question"]


# ----- D. turn context: measured, not shipped -----


@pytest.mark.asyncio
async def test_live_nlu_prompt_has_no_turn_context(tmp_path: Path) -> None:
    """The context block and its sentence were dropped (no measured gain)."""
    orch, llm = await _min_180_acked(tmp_path)
    llm.enqueue("nlu", _nlu(("min_payment_cents", 25000, "$250"), stance="counter"))
    await orch.on_creditor_text("Sorry, can you change it to $250?")
    system, user = (m["content"] for m in llm.nlu_messages[-1])
    assert "Earlier turns" not in user and "$180" not in user
    assert "earlier turns" not in system.lower()
    assert user.startswith("Today's date: ") and "Agent last said: " in user
    assert user == nlu_messages(
        "Sorry, can you change it to $250?",
        FIELDS_BY_NAME["payment_structure"].ask_text,
        None,
        ref=orch._ref,
    )[1]["content"]


@pytest.mark.asyncio
async def test_context_quote_is_not_verified(tmp_path: Path) -> None:
    """A value the model lifts from an earlier turn fails the verbatim-quote check."""
    orch, llm = await _min_180_acked(tmp_path)
    llm.enqueue(
        "nlu",
        _nlu(("min_payment_cents", 18000, "$180"), ("payment_structure", "even", "even")),
    )
    await orch.on_creditor_text("We need even payments.")
    assert orch.session.belief.get("min_payment_cents").value == 18000
    terms = [e["payload"]["terms"] for e in _events(orch, "analysis")][-1]
    assert [t["field"] for t in terms] == ["payment_structure"]
