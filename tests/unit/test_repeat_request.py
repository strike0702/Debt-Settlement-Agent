"""Phase 73: a repeat request says the last move again and changes nothing.

The act and its code cue, the policy re-say (second repeat shorter, no state
change, a line with content is handled by that content), and the natural
schedule read-out with its exact sentences.
"""

from __future__ import annotations

import copy
from datetime import date

import pytest

from app.agent.guards import template_guard
from app.agent.nlg import render_action
from app.agent.nlu.acts import verify_acts
from app.agent.nlu.context import CallContext
from app.agent.nlu.cues import repeat_cue
from app.agent.orchestrator import Orchestrator
from app.agent.policy import Intent, NegotiationState, Phase, decide
from app.agent.policy.moves import speak_schedule_action
from app.agent.policy.state import REPEAT_REASON, REPEAT_SHORT_REASON
from app.agent.session import CallSession
from app.config import Settings
from app.domain.actions import Action
from app.domain.belief import BeliefState
from app.domain.facts import Fact
from app.domain.nlu_types import (
    PriceAskAct,
    RepeatRequestAct,
    RepTurn,
    TermAnswerAct,
    UnclearAct,
)
from app.domain.scenario import load_scenario, resolve_scenario_dir
from feasibility.engine import ScheduleRow
from tests.wsutil import offline_settings

_REF = date(2026, 10, 9)
_S = Settings(hostility_threshold=0.8, max_turns=24)


def _belief() -> BeliefState:
    return BeliefState(load_scenario("fixtures/demo").client)


_REPEAT = RepTurn(acts=[RepeatRequestAct(type="repeat_request", quote="come again")])


def _pct(bp: int, fid: str = "counter_pct") -> Fact:
    return Fact(id=fid, kind="pct", value=bp, visibility="PUBLIC", source="engine")


def _offer() -> Action:
    total = Fact(id="offer_total", kind="money", value=65000, visibility="PUBLIC", source="engine")
    return Action(
        intent=Intent.COUNTER,
        facts={"counter_pct": _pct(5200), "offer_total": total},
        required={"counter_pct", "offer_total"},
        next_phase=Phase.NEGOTIATE,
        reason="bp=5200",
    )


@pytest.mark.parametrize(
    "line",
    [
        "Sorry. Can you come again?",
        "come again?",
        "sorry?",
        "Pardon?",
        "say that again",
        "What was that?",
        "What did you propose last",
        "what did you say?",
        "Can you repeat the number?",
        "Could you say that one more time please?",
    ],
)
def test_cue_adds_the_act_when_the_model_missed_it(line: str) -> None:
    verified = verify_acts([UnclearAct(type="unclear")], line, ref=_REF)
    assert repeat_cue(line)
    assert verified.to_rep_turn().repeat_only, line


@pytest.mark.parametrize(
    "line",
    [
        "Sorry.",
        "What did you say about the fees?",
        "Say that again and I am hanging up.",
        "What is the balance?",
        "Can you repeat the payment dates?",
        "sorry? oh, seventy five then",
    ],
)
def test_cue_stays_off_a_line_with_more_in_it(line: str) -> None:
    assert not repeat_cue(line)


def test_a_price_in_the_line_decides_and_the_repeat_is_ignored() -> None:
    line = "sorry? oh, seventy five percent then"
    acts = [
        RepeatRequestAct(type="repeat_request", quote="sorry?"),
        PriceAskAct(type="price_ask", pct=75, quote="seventy five percent"),
    ]
    verified = verify_acts(acts, line, ref=_REF)
    assert verified.settlement_ask_pct == 75
    assert not verified.has("repeat_request")


def test_model_repeat_over_an_unread_number_asks_instead_of_re_saying() -> None:
    line = "what did you say, sixty five?"
    acts = [RepeatRequestAct(type="repeat_request", quote="what did you say")]
    assert verify_acts(acts, line, ref=_REF).to_rep_turn().unclear


def test_one_amount_answers_an_open_tier_clarify_whatever_the_model_read() -> None:
    line = "I mean, one eighty dollars."
    acts = [TermAnswerAct(type="answer", field="min_payment_tiers", value="x", quote="one eighty")]
    open_clarify = CallContext(tier_clarify=True)
    verified = verify_acts(acts, line, ref=_REF, context=open_clarify)
    assert [(t.field, t.value) for t in verified.terms] == [("min_payment_cents", 18000)]
    assert not verify_acts(acts, line, ref=_REF).terms
    assert "tier_clarify" not in open_clarify.prompt_json()


def test_re_say_keeps_the_move_and_the_second_repeat_is_shorter() -> None:
    neg = NegotiationState(phase=Phase.NEGOTIATE, last_move=_offer())
    full = decide(_belief(), neg, _REPEAT, None, settings=_S)
    assert (full.intent, full.reason, full.effects) == (Intent.COUNTER, REPEAT_REASON, [])
    assert full.facts == _offer().facts
    assert full.next_phase == Phase.NEGOTIATE
    neg.repeat_streak = 1
    short = decide(_belief(), neg, _REPEAT, None, settings=_S)
    assert (short.intent, short.reason) == (Intent.COUNTER, REPEAT_SHORT_REASON)
    assert render_action(short, _REF) == ["Our offer is 52% of the balance, which is $650."]


def test_first_turn_re_asks_the_opening() -> None:
    action = decide(_belief(), NegotiationState(), _REPEAT, None, settings=_S)
    assert action.intent == Intent.OPENING
    assert action.reason == REPEAT_REASON


def test_guards_do_not_count_a_repeat() -> None:
    """A run of turns with no progress and a question at its cap still gets the re-say."""
    ask = Action(intent=Intent.ASK, next_phase=Phase.DISCOVERY, reason="max_payments",
                 text_slots={"ask_text": "How many payments?"})  # fmt: skip
    neg = NegotiationState(
        phase=Phase.DISCOVERY, last_move=ask, no_progress_turns=99,
        question_counts={"ASK:max_payments": 99},
    )  # fmt: skip
    action = decide(_belief(), neg, _REPEAT, None, settings=_S)
    assert (action.intent, action.reason) == (Intent.ASK, REPEAT_REASON)
    assert not any(e.kind == "note_question" for e in action.effects)


def test_after_the_proposal_a_repeat_re_says_it_without_reopening() -> None:
    proposal = Action(intent=Intent.PROPOSE_WRAP, next_phase=Phase.WRAP, reason="confirmed")
    neg = NegotiationState(phase=Phase.WRAP, last_move=proposal)
    action = decide(_belief(), neg, _REPEAT, None, settings=_S)
    assert (action.intent, action.reason) == (Intent.PROPOSE_WRAP, REPEAT_REASON)
    assert action.next_phase == Phase.WRAP
    assert action.effects == []


def _orchestrator() -> Orchestrator:
    scenario = load_scenario(resolve_scenario_dir("easy_deal"), rebase_to=_REF)
    return Orchestrator(
        CallSession(scenario=scenario), settings=offline_settings(), auto_ack=True
    )


@pytest.mark.asyncio
async def test_orchestrator_re_say_changes_no_state_and_keeps_a_read_back_pending() -> None:
    orch = _orchestrator()
    await orch.start()
    neg = orch.session.neg
    neg.pending_readback = "max_payments"
    neg.no_progress_turns = 1
    neg.last_question_key = "READ_BACK:max_payments"
    neg.question_counts = {"READ_BACK:max_payments": 1}
    neg.last_move = Action(
        intent=Intent.READ_BACK,
        facts={
            "readback_value": Fact(
                id="readback_value", kind="count", value=6, visibility="PUBLIC", source="creditor"
            )
        },
        text_slots={"field_label": "maximum number of payments"},
        next_phase=Phase.DISCOVERY,
        reason="max_payments",
    )
    before = copy.deepcopy(neg)
    utt = await orch.on_creditor_text("Sorry?", oracle=_REPEAT)
    assert [t for _, t in utt.sentences] == [
        "Sure.",
        "So I have 6 for the maximum number of payments.",
        "Is that right?",
    ]
    after = orch.session.neg
    assert after.repeat_streak == 1
    assert after.turn_idx == before.turn_idx + 1
    after.repeat_streak, after.turn_idx = before.repeat_streak, before.turn_idx
    assert after == before


@pytest.mark.asyncio
async def test_a_repeat_after_the_schedule_read_says_the_same_sentence() -> None:
    orch = _orchestrator()
    await orch.start()
    action = speak_schedule_action(_rows([21666, 21667], [date(2026, 11, 30), date(2026, 12, 31)]))
    sentence = render_action(action, _REF)
    orch.session.neg.phase = Phase.CONFIRM
    orch._note_heard(action)
    utt = await orch.on_creditor_text("come again?", oracle=_REPEAT)
    assert [t for _, t in utt.sentences] == ["Sure.", *sentence]
    assert utt.action.next_phase == Phase.CONFIRM


def _rows(cents: list[int], dates: list[date]) -> list[ScheduleRow]:
    return [
        ScheduleRow(
            date=d, creditor_payment_cents=c, program_fee_cents=0, bank_fee_cents=0, balance_cents=0
        )
        for c, d in zip(cents, dates, strict=True)
    ]


def _monthly(n: int) -> list[date]:
    months = [
        (2026, 11, 30), (2026, 12, 31), (2027, 1, 31), (2027, 2, 28), (2027, 3, 31),
        (2027, 4, 30), (2027, 5, 31), (2027, 6, 30), (2027, 7, 31), (2027, 8, 31),
    ]  # fmt: skip
    return [date(*m) for m in months[:n]]


_AMT = 21667


@pytest.mark.parametrize(
    ("cents", "dates", "sentence"),
    [
        (
            [_AMT, _AMT, _AMT],
            _monthly(3),
            "That's 3 payments of $216.67 each, on November 30, December 31 and January 31, 2027.",
        ),
        (
            [21666, _AMT, _AMT],
            _monthly(3),
            "That's 3 payments: $216.66 on November 30, then $216.67 on December 31 "
            "and January 31, 2027.",
        ),
        (
            [20000] * 5,
            _monthly(5),
            "That's 5 payments of $200 each, monthly from November 30 to March 31, 2027.",
        ),
        ([50000], _monthly(1), "That's a single payment of $500 on November 30."),
        (
            [10000] * 5 + [90000],
            _monthly(6),
            "That's 6 payments: 5 of $100 each, monthly from November 30 to March 31, 2027, "
            "then $900 on April 30, 2027.",
        ),
        (
            [10000] * 3 + [20000] * 3 + [30000] * 3,
            _monthly(9),
            "That's 9 payments, monthly from November 30 to July 31, 2027: 3 of $100 each, "
            "then 3 of $200 each, then 3 of $300 each.",
        ),
        (
            [10000, 11000, 12000, 13000, 14000, 15000],
            _monthly(6),
            "That's 6 payments, monthly from November 30 to April 30, 2027, each between "
            "$100 and $150, for $750 in all.",
        ),
    ],
)
def test_schedule_read_out_sentences(cents: list[int], dates: list[date], sentence: str) -> None:
    action = speak_schedule_action(_rows(cents, dates))
    assert action.template_override is not None
    guard = template_guard(action.template_override, set(action.facts), action.required)
    assert guard.ok, guard
    assert " ".join(render_action(action, _REF)) == sentence
