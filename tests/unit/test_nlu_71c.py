"""Phase 71c: fixes from the live call of 2026-11-08.

- A reply with several JSON objects (the model corrected itself) uses the last
  one that validates.
- An empty tier list from the model in reply to the optional-rules sweep
  ("No, nothing like that.") is dropped, not read back.
- A reply to the ``tiers_inconsistent`` clarify that gives a minimum corrects
  that tier and asks for the next one; it does not restart the tier list.

(WRAP never reopening is in ``test_policy_acts.py`` next to the grid.)
"""

from __future__ import annotations

from dataclasses import replace

from app.agent.nlu.acts import parse_acts, verify_acts
from app.agent.policy import Intent, NegotiationState, Phase, decide
from app.agent.policy.moves import SWEEP_ASK_TEXT
from app.config import Settings
from app.domain.belief import BeliefState
from app.domain.nlu_types import RepTurn, RuleItemAct, StateRuleAct, TermAnswerAct
from app.domain.scenario import load_scenario

_LIVE_REPLY = (
    '{"acts":[{"type":"respond_to_offer","response":"reject","quote":"No."}]}\n'
    "Wait, that's not right. \"No\" here answers the agent's question about whether "
    "anything else is needed, which is not an offer response. Correcting my reply:\n"
    '{"acts":[]}'
)


def test_last_valid_json_object_wins_and_is_reported() -> None:
    acts, error = parse_acts(_LIVE_REPLY)
    assert acts.acts == []
    assert error is not None and error.startswith("used object 2 of 2")
    # A later object that does not validate does not replace a valid one.
    first_ok = _LIVE_REPLY.replace('{"acts":[]}', '{"acts":[{"type":"nope"}]}')
    acts, error = parse_acts(first_ok)
    assert [a.type for a in acts.acts] == ["respond_to_offer"]
    assert error is not None and error.startswith("used object 1 of 2")


def test_an_empty_tier_answer_is_no_rule_not_a_read_back() -> None:
    """Haiku sometimes answers the tiers sweep "No, nothing like that." with ``[]``."""
    empty = TermAnswerAct(type="answer", field="min_payment_tiers", value=[], quote="No")
    v = verify_acts([empty], "No, nothing like that.", last_agent_line=SWEEP_ASK_TEXT)
    assert v.to_rep_turn().terms == [] and v.to_rep_turn().tier_items() == []


_SETTINGS = Settings(max_turns=24)


def _belief() -> BeliefState:
    b = BeliefState(load_scenario("fixtures/demo").client)
    for field, value in (
        ("max_payments", 8),
        ("min_payment_cents", 18000),
        ("payment_structure", "flexible"),
    ):
        b.observe(field, value, "q", 1, verified=True, hedged=False)
    return b


def _apply_draft(neg: NegotiationState, effects: list) -> NegotiationState:
    e = next(x for x in effects if x.kind == "set_tier_draft")
    pending = e.data.get("pending")
    return replace(
        neg,
        tier_draft=[tuple(t) for t in e.data["tiers"]],
        tier_ends={int(k): v for k, v in (e.data.get("ends") or {}).items()},
        tier_pending=None if pending is None else tuple(pending),
    )


def test_tier_clarify_answer_corrects_the_tier_and_asks_the_next() -> None:
    belief = _belief()
    neg = NegotiationState(phase=Phase.DISCOVERY, turn_idx=4, tier_draft=[], tier_expected=3)
    # "It starts from one goes on till the second payment, and the minimum is $100."
    item = RuleItemAct(type="rule_item", from_payment=1, to_payment=2, min_cents=10000, quote="q")
    clarify = decide(belief, neg, RepTurn(acts=[item]), None, settings=_SETTINGS)
    assert (clarify.intent, clarify.reason) == (Intent.CLARIFY, "tiers_inconsistent")
    neg = _apply_draft(neg, clarify.effects)
    assert neg.tier_draft == [] and neg.tier_pending == (1, 2, 10000)
    # "I mean, one eighty dollars."
    answer = StateRuleAct(
        type="state_rule", field="min_payment_cents", value=18000, quote="one eighty dollars"
    )
    nxt = decide(belief, neg, RepTurn(acts=[answer]), None, settings=_SETTINGS)
    assert (nxt.intent, nxt.reason) == (Intent.ASK, "min_payment_tiers")
    after = _apply_draft(neg, nxt.effects)
    assert after.tier_draft == [(1, 18000)] and after.tier_ends == {1: 2}
    assert after.tier_pending is None
    assert not nxt.facts.get("tier_position")  # "tier 2", not "Starting with tier 1"
