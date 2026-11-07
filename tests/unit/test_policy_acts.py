"""Phase 71b: the policy reads NLU acts directly.

The grid: every (phase × act type) pair, plus the empty act list, yields a
legal move with a documented reason code. Then the rules that are new or
moved: the multi-act conflict rule, ``unclear`` / unusable-act fallbacks,
tier ranges checked across turns, a rule revision after a proposal that
names the old value (held-out ``neg_rule_change`` #2), and the corpus
context for a pending read-back.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.adapter.engine_adapter import Affordability
from app.agent.nlg import SAFE_FALLBACK, render_action
from app.agent.orchestrator import Orchestrator
from app.agent.policy import DISPATCH_ORDER, Intent, NegotiationState, Phase, decide
from app.agent.policy.moves import _confirm_key
from app.agent.reasons import REASON_TEXT, reason_key
from app.agent.session import CallSession
from app.config import Settings
from app.domain.belief import BeliefState, TermStatus
from app.domain.nlu_types import (
    ACT_TYPES,
    AnnounceRulesAct,
    AskPrivateInfoAct,
    BackchannelAct,
    DemandCommitmentAct,
    EndCallAct,
    HostilityAct,
    ListDoneAct,
    PriceAskAct,
    QuestionAct,
    ReadbackReplyAct,
    RepTurn,
    RespondToOfferAct,
    RuleItemAct,
    StallAct,
    StateRuleAct,
    TermAnswerAct,
    UnclearAct,
)
from app.domain.scenario import load_scenario
from eval.nlu_corpus import line_call_context
from tests.acts_helpers import acts_turn, rule_act
from tests.wsutil import offline_settings

_REF = date(2026, 3, 1)
_SETTINGS = Settings(
    hostility_threshold=0.8, max_turns=24, max_counters=4, anchor_ratio=0.7, concession_factor=0.5
)
_Q = "line"

# One sample act per type (values a rep could say on any call).
_SAMPLES: dict[str, Any] = {
    "answer": TermAnswerAct(type="answer", field="max_payments", value=6, quote=_Q),
    "state_rule": StateRuleAct(type="state_rule", field="min_payment_cents", value=15000, quote=_Q),
    "announce_rules": AnnounceRulesAct(
        type="announce_rules", field="min_payment_tiers", count=2, quote=_Q
    ),
    "rule_item": RuleItemAct(
        type="rule_item", from_payment=3, to_payment=5, min_cents=25000, quote=_Q
    ),
    "list_done": ListDoneAct(type="list_done", quote=_Q),
    "price_ask": PriceAskAct(type="price_ask", pct=55.0, quote=_Q),
    "respond_to_offer": RespondToOfferAct(type="respond_to_offer", response="accept", quote=_Q),
    "readback_reply": ReadbackReplyAct(type="readback_reply", response="confirm", quote=_Q),
    "question": QuestionAct(type="question", topic="next_steps", quote=_Q),
    "ask_private_info": AskPrivateInfoAct(type="ask_private_info", quote=_Q),
    "demand_commitment": DemandCommitmentAct(type="demand_commitment", quote=_Q),
    "hostility": HostilityAct(type="hostility", level="strong", quote=_Q),
    "end_call": EndCallAct(type="end_call", kind="goodbye", quote=_Q),
    "stall": StallAct(type="stall", quote=_Q),
    "backchannel": BackchannelAct(type="backchannel", quote=_Q),
    "unclear": UnclearAct(type="unclear", quote=_Q),
}


def _belief(rules: bool = True) -> BeliefState:
    b = BeliefState(load_scenario("fixtures/demo").client)
    if rules:
        for field, value in (
            ("max_payments", 6),
            ("min_payment_cents", 10000),
            ("payment_structure", "even"),
        ):
            b.observe(field, value, "q", 1, verified=True, hedged=False)
    return b


def _afford(max_bp: int = 5000) -> Affordability:
    bps = list(range(100, max_bp + 1, 100))
    curve = tuple(bp in set(bps) for bp in range(100, 10001, 100))
    return Affordability(max_bp=max_bp, feasible_bps=bps, curve=curve)


def _states() -> dict[str, tuple[BeliefState, NegotiationState, Affordability | None]]:
    """One call state per phase, plus two DISCOVERY sub-states (a list open, a read-back)."""
    full = _belief()
    confirm = NegotiationState(
        phase=Phase.CONFIRM, turn_idx=6, ask_bp=4500, confirmed_bp=4500,
        last_confirm_key=_confirm_key(full, 4500), counters_offered=[3500], counter_turns=[4],
    )  # fmt: skip
    return {
        "OPENING": (_belief(False), NegotiationState(phase=Phase.OPENING, turn_idx=1), None),
        "DISCOVERY": (
            _belief(False),
            NegotiationState(
                phase=Phase.DISCOVERY, turn_idx=2, last_question_key="ASK:max_payments"
            ),
            None,
        ),
        "DISCOVERY_list": (
            full,
            NegotiationState(
                phase=Phase.DISCOVERY, turn_idx=3, tier_draft=[(1, 15000)], tier_expected=3
            ),
            None,
        ),
        "DISCOVERY_readback": (
            full,
            NegotiationState(phase=Phase.DISCOVERY, turn_idx=3, pending_readback="max_payments"),
            None,
        ),
        "NEGOTIATE": (
            full,
            NegotiationState(
                phase=Phase.NEGOTIATE, turn_idx=5, ask_bp=6000, counters_offered=[3500],
                counter_turns=[4], ask_at_last_counter=6000,
            ),  # fmt: skip
            _afford(),
        ),
        "CONFIRM": (full, confirm, _afford()),
        "WRAP": (
            full, NegotiationState(phase=Phase.WRAP, turn_idx=8, confirmed_bp=4500), _afford()
        ),
        "END": (full, NegotiationState(phase=Phase.END, turn_idx=9), _afford()),
        "ESCALATE": (full, NegotiationState(phase=Phase.ESCALATE, turn_idx=9), _afford()),
    }


_CELLS = [(s, a) for s in _states() for a in [*ACT_TYPES, None]]


def _decide(state: str, act_type: str | None) -> Any:
    belief, neg, afford = _states()[state]
    turn = RepTurn(acts=[] if act_type is None else [_SAMPLES[act_type]])
    if afford is None and turn.ask_pct is not None and not belief.missing_required():
        afford = _afford()
    return decide(belief, neg, turn, afford, settings=_SETTINGS)


def test_every_act_type_has_a_sample_and_a_place_in_the_dispatch_order() -> None:
    assert set(_SAMPLES) == set(ACT_TYPES)
    assert set(ACT_TYPES) <= {name for name, _ in DISPATCH_ORDER}


@pytest.mark.parametrize(("state", "act_type"), _CELLS)
def test_grid_every_phase_and_act_yields_a_legal_move(state: str, act_type: str | None) -> None:
    """A move, never NO_DEAL_WRAP, with a documented reason code, speakable by the guards."""
    action = _decide(state, act_type)
    assert action.intent != Intent.NO_DEAL_WRAP
    key = reason_key(action.intent, action.reason)
    assert key in REASON_TEXT, (state, act_type, action.intent, action.reason)
    if action.intent not in (Intent.COUNTER, Intent.CONFIRM_SCHEDULE, Intent.PROPOSE_WRAP):
        # Price moves get their engine facts in the orchestrator; the rest render here.
        assert render_action(action, _REF) != [SAFE_FALLBACK], (state, act_type)


@pytest.mark.parametrize("state", ["OPENING", "DISCOVERY", "NEGOTIATE", "CONFIRM", "WRAP"])
def test_unclear_is_asked_again_in_every_live_phase(state: str) -> None:
    action = _decide(state, "unclear")
    assert (action.intent, action.reason) == (Intent.CLARIFY, "unclear")


# 71c: after the proposal only close, read the schedule, ask again, hand off, refuse.
_WRAP_MOVES = {
    Intent.CLOSE, Intent.SPEAK_SCHEDULE, Intent.CLARIFY, Intent.ESCALATE,
    Intent.REFUSE_PRIVATE, Intent.REFUSE_COMMIT,
}  # fmt: skip


@pytest.mark.parametrize("act_type", [*ACT_TYPES, None])
def test_wrap_never_reopens_discovery_or_the_ladder(act_type: str | None) -> None:
    action = _decide("WRAP", act_type)
    assert action.intent in _WRAP_MOVES, (act_type, action.intent)
    assert action.next_phase in (Phase.WRAP, Phase.END, Phase.ESCALATE), act_type
    assert not any(e.kind == "set_phase" and e.data["phase"] in ("DISCOVERY", "NEGOTIATE",
                   "CONFIRM") for e in action.effects)  # fmt: skip


def test_no_to_anything_else_closes_and_a_change_hands_off() -> None:
    """The live call: "No." (read as a reject, or nothing) after the proposal closes."""
    belief, neg, afford = _states()["WRAP"]
    reject = RepTurn(acts=[RespondToOfferAct(type="respond_to_offer", response="reject", quote=_Q)])
    for turn in (reject, RepTurn(acts=[])):
        action = decide(belief, neg, turn, afford, settings=_SETTINGS)
        assert (action.intent, action.reason) == (Intent.CLOSE, "post_wrap")
    price = decide(belief, neg, RepTurn(acts=[_SAMPLES["price_ask"]]), afford, settings=_SETTINGS)
    assert (price.intent, price.reason) == (Intent.ESCALATE, "changed_after_proposal")


@pytest.mark.parametrize("state", ["DISCOVERY", "NEGOTIATE", "CONFIRM"])
def test_an_act_the_phase_cannot_use_is_clarified_not_dropped(state: str) -> None:
    """"That's all" with no list open is asked about, never silently ignored."""
    action = _decide(state, "list_done")
    assert (action.intent, action.reason) == (Intent.CLARIFY, "act_unusable")
    # With a list open it closes the list (read-back), not the call.
    listed = _decide("DISCOVERY_list", "list_done")
    assert (listed.intent, listed.reason) == (Intent.READ_BACK, "min_payment_tiers")


def test_safety_acts_win_in_every_phase() -> None:
    for state in _states():
        assert _decide(state, "hostility").reason == "hostile", state


# --- multi-act lines -------------------------------------------------------------


def test_accept_outranks_a_goodbye_in_the_same_line() -> None:
    belief, neg, afford = _states()["CONFIRM"]
    both = acts_turn(stance="accept", wants_to_end=True)
    assert decide(belief, neg, both, afford, settings=_SETTINGS).intent == Intent.PROPOSE_WRAP
    bye = acts_turn(wants_to_end=True)
    assert decide(belief, neg, bye, afford, settings=_SETTINGS).reason == "thanks_accept"


def test_accept_with_a_different_number_is_that_number_not_a_yes() -> None:
    belief, neg, afford = _states()["CONFIRM"]
    turn = acts_turn(stance="accept", settlement_ask_pct=50.0, ask_quote="fifty")
    action = decide(belief, neg, turn, afford, settings=_SETTINGS)
    assert action.intent != Intent.PROPOSE_WRAP


def test_tier_and_list_end_in_one_line_close_the_list_not_the_call() -> None:
    belief, neg, _ = _states()["DISCOVERY_list"]
    turn = RepTurn(
        acts=[
            _item(2, None, 20000),
            ListDoneAct(type="list_done", quote=_Q),
            EndCallAct(type="end_call", quote=_Q),
        ]
    )
    action = decide(belief, neg, turn, None, settings=_SETTINGS)
    assert (action.intent, action.reason) == (Intent.READ_BACK, "min_payment_tiers")
    draft = next(e for e in action.effects if e.kind == "set_tier_draft")
    assert draft.data["tiers"] == [[1, 15000], [2, 20000]]


# --- tier ranges across turns ----------------------------------------------------


def test_tier_ranges_are_checked_across_turns() -> None:
    belief = _belief()
    neg = NegotiationState(phase=Phase.DISCOVERY, turn_idx=3, tier_draft=[], tier_expected=3)
    first = RepTurn(acts=[_item(1, 2, 19000)])
    a1 = decide(belief, neg, first, None, settings=_SETTINGS)
    eff = next(e for e in a1.effects if e.kind == "set_tier_draft")
    assert eff.data["ends"] == {"1": 2}
    neg = NegotiationState(
        phase=Phase.DISCOVERY, turn_idx=4, tier_draft=[(1, 19000)], tier_ends={1: 2},
        tier_expected=3,
    )  # fmt: skip
    # "Payments four to five": payment 3 has no tier → asked, not merged.
    gap = decide(belief, neg, RepTurn(acts=[_item(4, 5, 25000)]), None, settings=_SETTINGS)
    assert (gap.intent, gap.reason) == (Intent.CLARIFY, "tiers_gap")
    kept = next(e for e in gap.effects if e.kind == "set_tier_draft")
    assert kept.data["tiers"] == [[1, 19000]]
    # "Payments three to five" joins up.
    ok = decide(belief, neg, RepTurn(acts=[_item(3, 5, 25000)]), None, settings=_SETTINGS)
    assert (ok.intent, ok.reason) == (Intent.ASK, "min_payment_tiers")


def _item(frm: int, to: int | None, cents: int) -> RuleItemAct:
    return RuleItemAct(type="rule_item", from_payment=frm, to_payment=to, min_cents=cents, quote=_Q)


# --- rule revision after a proposal (held-out neg_rule_change #2) -------------------


@pytest.mark.asyncio
async def test_rule_change_naming_the_old_value_is_a_revision_not_a_clarify() -> None:
    orch = Orchestrator(
        CallSession(scenario=load_scenario("fixtures/scenarios/easy_deal")),
        settings=offline_settings(),
        auto_ack=True,
    )
    await orch.start()
    terms = [
        rule_act("max_payments", 8, "eight payments"),
        rule_act("min_payment_cents", 10000, "$100"),
        rule_act("payment_structure", "even", "even payments"),
    ]
    await orch.on_creditor_text(
        "Up to eight payments, at least $100 each, even payments.",
        oracle=acts_turn(terms=terms),
    )
    first = await orch.on_creditor_text(
        "We'd take forty five percent.",
        oracle=acts_turn(settlement_ask_pct=45.0, ask_quote="forty five percent"),
    )
    assert first.action.intent == Intent.COUNTER
    line = "Actually, I just checked, on this account it's six payments max, not eight."
    utt = await orch.on_creditor_text(
        line, oracle=acts_turn(terms=[rule_act("max_payments", 6, "six payments max")])
    )
    assert (utt.action.intent, utt.action.reason) == (Intent.COUNTER, "hold")
    term = orch.session.belief.get("max_payments")
    assert (term.value, term.status) == (6, TermStatus.KNOWN)


def test_corpus_context_awaits_the_pending_readback() -> None:
    ctx = line_call_context({"text": "Yes.", "pending": "max_payments"})
    assert (ctx.awaiting, ctx.awaiting_field) == ("readback", "max_payments")
    assert line_call_context({"text": "Yes."}).awaiting == "none"
