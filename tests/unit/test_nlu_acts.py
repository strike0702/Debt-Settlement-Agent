"""NLU v2 acts (Phase 71a): validation, call context privacy, tier items, accounting, converter.

- An unknown act type or field fails validation and becomes one ``unclear`` act;
  the policy then asks the rep to rephrase.
- The call context built on every scenario's autoplay never carries a PRIVATE
  value into the acts prompt (the session's private blocklist, the client's
  maximum and the accept line).
- Tier ``rule_item``s are read by code from the rep's words; ranges in one line
  must be contiguous.
- Number accounting: the 73-vs-75 line with no price act leaves "seventy three"
  unaccounted and the policy asks for the number instead of accepting 75%.
- "That's all the tiers" while tiers are collected closes the list, not the call.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

import app.agent.orchestrator as orch_mod
from app.agent.nlu import CallContext, build_call_context, convert_acts, parse_acts
from app.agent.nlu.context import ListState
from app.agent.nlu_types import TurnAnalysis
from app.agent.numbers import extract_tokens
from app.agent.policy import Intent, NegotiationState, Phase, decide
from app.autoplay import new_autoplay_call, run_autoplay
from app.domain.negotiation import accept_line_bp
from app.domain.nlu_types import ActList
from app.domain.scenario import load_scenario, resolve_scenario_dir
from app.llm.prompts import nlu_acts_messages
from tests.wsutil import offline_settings

_REF = date(2026, 3, 1)
_SETTINGS = offline_settings()
_SCENARIOS = sorted(p.name for p in resolve_scenario_dir("easy_deal").parent.iterdir())


def _acts(*acts: dict) -> ActList:
    return ActList.model_validate({"acts": list(acts)})


@pytest.mark.parametrize(
    "reply",
    [
        '{"acts":[{"type":"haggle","quote":"x"}]}',
        '{"acts":[{"type":"stall","quote":"hold on","extra":1}]}',
        '{"acts":[{"type":"price_ask","quote":"fifty","pct":50,"total_cents":100}]}',
        '{"acts":[],"stance":"info"}',
        "not json",
    ],
)
def test_invalid_reply_becomes_one_unclear_act_and_a_rephrase(reply: str) -> None:
    acts, error = parse_acts(reply)
    assert error is not None and [a.type for a in acts.acts] == ["unclear"]
    out = convert_acts(acts, "Blah blah whatever.", ref=_REF)
    assert out.unclear and out.acts == ["unclear"]
    action = decide(_belief(), NegotiationState(), out.to_turn_analysis(), None, settings=_SETTINGS)
    assert (action.intent, action.reason) == (Intent.CLARIFY, "unclear")


def _belief():  # noqa: ANN202
    from app.domain.belief import BeliefState

    return BeliefState(load_scenario("fixtures/demo").client)


@pytest.mark.parametrize("sid", _SCENARIOS)
async def test_call_context_never_carries_private_values(
    sid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = load_scenario(resolve_scenario_dir(sid), rebase_to=date(2026, 10, 9))
    orch, creditor = new_autoplay_call(call, sid, settings=_SETTINGS, audit=None)
    contexts: list[CallContext] = []
    maxes: set[int] = set()

    def spy(belief, neg):  # noqa: ANN001, ANN202
        ctx = build_call_context(belief, neg)
        contexts.append(ctx)
        if orch.session.last_max_bp is not None:
            maxes.add(orch.session.last_max_bp)
        return ctx

    monkeypatch.setattr(orch_mod, "build_call_context", spy)

    async def noop(_x) -> None:  # noqa: ANN001
        return None

    await run_autoplay(orch, creditor, on_agent=noop, on_creditor=noop)
    assert contexts
    private = set(orch.session.private_blocklist)
    for bp in maxes:
        private |= {("pct", bp), ("pct", accept_line_bp(bp, _SETTINGS.accept_line_pct_of_max_bp))}
    # A value the rep said is public even when it equals a private one (the
    # rendered guard makes the same exception).
    private -= set(orch.session.creditor_numbers)
    offered = set(orch.session.neg.counters_offered) | {orch.session.neg.confirmed_bp}
    for ctx in contexts:
        system, user = nlu_acts_messages("", "", ctx.prompt_json(), ref=_REF)
        block = user["content"]
        # Context numbers are cents / percent points: check both raw and rendered.
        for kind, value in private:
            if kind == "money":
                assert not re.search(rf"[:\[,]{value}[,\]}}]", block), (sid, value)
            if kind == "pct" and value not in offered:
                assert f"{value / 100}" not in block, (sid, value)
        tokens = {(t.kind, t.value) for t in extract_tokens(block)}
        assert not tokens & private, sid
        assert system["content"].startswith("You read one line")


def test_tier_items_are_read_by_code_and_must_be_contiguous() -> None:
    line = "The first two payments must be at least one hundred ninety dollars."
    acts = _acts(
        {"type": "rule_item", "from_payment": 1, "to_payment": 3, "min_cents": 1, "quote": line}
    )
    ctx = CallContext(awaiting="list_item", collecting=ListState(expected=3))
    out = convert_acts(acts, line, context=ctx, ref=_REF)
    tiers = next(t for t in out.terms if t.field == "min_payment_tiers")
    assert tiers.value == [(1, 19000)] and not out.tiers_ambiguous
    assert out.unaccounted_numbers == []
    gap = "Payments one to two at least $190, and payments four to five at least $250."
    acts = _acts(
        {"type": "rule_item", "quote": "Payments one to two at least $190"},
        {"type": "rule_item", "quote": "payments four to five at least $250"},
    )
    out = convert_acts(acts, gap, context=ctx, ref=_REF)
    assert out.tiers_ambiguous and not any(t.field == "min_payment_tiers" for t in out.terms)


def test_firm_line_with_an_unread_number_never_reuses_the_old_ask() -> None:
    line = "I think seventy three is the lowest I can go"
    neg = NegotiationState(
        phase=Phase.NEGOTIATE, ask_bp=7500, counters_offered=[6000], final_counter_ask=7500
    )
    ctx = build_call_context(_belief(), neg)
    out = convert_acts(_acts({"type": "stall", "quote": "I think"}), line, context=ctx, ref=_REF)
    assert out.firm and out.unaccounted_numbers == ["seventy three"]
    action = decide(_belief(), neg, out.to_turn_analysis(), None, settings=_SETTINGS)
    assert (action.intent, action.reason) == (Intent.CLARIFY, "unaccounted_number")
    read = convert_acts(
        _acts({"type": "price_ask", "pct": 73, "firmness": "final", "quote": line}),
        line,
        context=ctx,
        ref=_REF,
    )
    assert read.settlement_ask_pct == 73 and read.firm and read.unaccounted_numbers == []
    # Flat schema / oracle analyses never carry the new fields: no policy change there.
    assert TurnAnalysis(firm=True).unaccounted_numbers == []


def test_thats_all_while_collecting_tiers_closes_the_list_not_the_call() -> None:
    line = "That's all the tiers, thanks."
    acts = _acts(
        {"type": "list_done", "quote": "That's all the tiers"},
        {"type": "end_call", "kind": "goodbye", "quote": "thanks"},
    )
    collecting = CallContext(
        awaiting="list_item", collecting=ListState(have=[(1, 19000), (3, 25000)])
    )
    out = convert_acts(acts, line, context=collecting, ref=_REF)
    assert out.list_done and not out.wants_to_end
    # Outside a list, a goodbye still ends the call.
    out = convert_acts(acts, line, context=CallContext(), ref=_REF)
    assert out.wants_to_end


def test_call_context_marks_operator_rules_until_the_rep_states_them() -> None:
    belief = _belief()
    belief.seed_operator("max_payments", 8)
    ctx = build_call_context(belief, NegotiationState(phase=Phase.DISCOVERY))
    assert ctx.known_rules["max_payments"] == {"value": 8, "source": "operator"}
