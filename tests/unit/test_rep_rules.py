"""The rep defines its own rules: yield, tiers one by one, read-back, sweep, loop guard.

- The live call of 2026-11-07 as a typed call (oracle NLU carries only the
  terms; the code-side cues read the announcements): the agent never asks the
  price while the rep is defining tiers, takes them one per turn, reads the
  set back once, and a plain no starts the list again.
- Partial tiers merge by starting payment; a tier past the max payments is
  clarified; the optional sweep runs only for flexible schedules; a question
  the rep did not answer is reworded, not repeated.
- The ``defines`` corpus lines are read by the code-side cue alone, and the
  ``tier_definer`` fixture autoplays to a deal on its tiers with a rep card
  that states the same rules and tiers as its ``sim.json``.
"""

from __future__ import annotations

import json
from datetime import date

from app.agent.nlg import TEMPLATES, render_action
from app.agent.nlu import repair_defines_field
from app.agent.orchestrator import Orchestrator
from app.agent.policy import Intent, NegotiationState, Phase, decide
from app.agent.session import CallSession
from app.autoplay import new_autoplay_call, run_autoplay
from app.domain.belief import BeliefState, TermStatus
from app.domain.nlu_types import RepTurn
from app.domain.rule_replies import merge_suggestions, rule_reply_lines
from app.domain.scenario import (
    load_scenario,
    rep_card_suggestions,
    resolve_scenario_dir,
)
from eval.nlu_corpus import load_corpus
from tests.acts_helpers import acts_turn, flag, rule_act, verify_turn
from tests.wsutil import offline_settings

_REF = date(2026, 3, 1)
_SETTINGS = offline_settings()
_TIERS = [(1, 19000), (3, 25000), (6, 30000)]


def _tier(frm: int, cents: int, quote: str) -> RepTurn:
    return acts_turn(
        stance="info",
        terms=[rule_act(field="min_payment_tiers", value=[(frm, cents)], quote=quote)],
    )


def _belief(structure: str = "flexible") -> BeliefState:
    b = BeliefState(load_scenario("fixtures/demo").client)
    for field, value in (
        ("max_payments", 8),
        ("min_payment_cents", 19000),
        ("payment_structure", structure),
    ):
        b.observe(field, value, str(value), 1, verified=True, hedged=False)
    return b


def _decide(line: str, neg: NegotiationState, *, belief: BeliefState | None = None):  # noqa: ANN202
    analysis = verify_turn(acts_turn(stance="info"), line, ref=_REF).to_rep_turn()
    return decide(belief or _belief(), neg, analysis, None, settings=_SETTINGS)


async def test_live_call_rep_defines_three_tiers_before_the_price() -> None:
    call = load_scenario("fixtures/scenarios/easy_deal", rebase_to=_REF)
    orch = Orchestrator(CallSession(scenario=call), settings=_SETTINGS, auto_ack=True)
    await orch.start()

    async def say(text: str, oracle: RepTurn | None = None) -> tuple[str, str | None, str]:
        utt = await orch.on_creditor_text(text, oracle=oracle or acts_turn(stance="info"))
        return utt.action.intent.value, utt.action.reason, " ".join(t for _, t in utt.sentences)

    rules = acts_turn(
        stance="info",
        terms=[
            rule_act(field="max_payments", value=8, quote="eight payments"),
            rule_act(field="min_payment_cents", value=19000, quote="$190"),
        ],
    )
    assert (await say("Up to eight payments, at least $190 each.", rules))[1] == (
        "payment_structure"
    )
    flexible = acts_turn(
        stance="info",
        terms=[rule_act(field="payment_structure", value="flexible", quote="flexible")],
    )
    _, reason, _ = await say(
        "I need a flexible schedule. I will define the payment structure.", flexible
    )
    assert reason == "optional_sweep"
    intent, reason, first_ask = await say("Uh, before that, I need to define the payment tiers.")
    assert (intent, reason) == ("ASK", "min_payment_tiers")
    intent, reason, count_ask = await say("So we will have three payment tiers.")
    assert (intent, reason) == ("ASK", "min_payment_tiers") and count_ask != first_ask
    assert orch.session.neg.tier_expected == 3
    lines = [
        "Payments 1 to 2, at least $190",
        "Then from the 3rd payment on, at least $250",
        "And from the 6th payment on, at least $300",
    ]
    moves = [await say(f"{q}.", _tier(*t, q)) for q, t in zip(lines, _TIERS, strict=True)]
    assert [m[0] for m in moves] == ["ASK", "ASK", "READ_BACK"]
    assert moves[2][2] == (
        "So payments 1 to 2 at least $190, 3 to 5 at least $250, 6 to 8 at least $300. "
        "Is that right?"
    )
    # Nothing reached belief before the yes; a plain no starts the list again.
    assert orch.session.belief.get("min_payment_tiers").status == TermStatus.ASSUMED
    intent, _, again = await say("No.", acts_turn(stance="info", readback_response="deny"))
    assert intent == "ASK" and again.startswith("Let us go through the tiers again")
    for q, t in zip(lines, _TIERS, strict=True):
        intent, _, _ = await say(f"{q}.", _tier(*t, q))
    assert intent == "READ_BACK"
    intent, _, _ = await say("Correct.", acts_turn(stance="info", readback_response="confirm"))
    tiers = orch.session.belief.get("min_payment_tiers")
    assert tiers.status == TermStatus.KNOWN and [tuple(x) for x in tiers.value] == _TIERS
    assert intent == "ASK_SETTLEMENT" and orch.session.neg.tier_draft is None


def test_partial_tiers_merge_and_a_restated_tier_replaces_its_minimum() -> None:
    neg = NegotiationState(phase=Phase.DISCOVERY, tier_draft=[(1, 19000)], tier_expected=3)
    action = decide(
        _belief(), neg, _tier(3, 25000, "from the 3rd payment, $250"), None, settings=_SETTINGS
    )
    assert action.intent == Intent.ASK and action.reason == "min_payment_tiers"
    draft = next(e for e in action.effects if e.kind == "set_tier_draft").data
    assert draft == {"tiers": [[1, 19000], [3, 25000]], "expected": 3, "readback": False}
    neg = NegotiationState(phase=Phase.DISCOVERY, tier_draft=[(1, 19000), (3, 25000)])
    action = decide(
        _belief(), neg, _tier(3, 26000, "from the 3rd payment, $260"), None, settings=_SETTINGS
    )
    draft = next(e for e in action.effects if e.kind == "set_tier_draft").data
    assert draft["tiers"] == [[1, 19000], [3, 26000]]
    # No count given: "that's all" ends the list and reads it back.
    action = _decide("That's all.", NegotiationState(tier_draft=[(1, 19000), (3, 26000)]))
    assert action.intent == Intent.READ_BACK


def test_tier_past_the_max_payments_is_clarified_and_dropped() -> None:
    neg = NegotiationState(phase=Phase.DISCOVERY, tier_draft=[(1, 19000)], tier_expected=2)
    action = decide(
        _belief(), neg, _tier(10, 30000, "from the 10th payment, $300"), None, settings=_SETTINGS
    )
    assert action.intent == Intent.CLARIFY and action.reason == "tiers_inconsistent"
    assert "at most 8 payments" in " ".join(render_action(action, _REF))
    draft = next(e for e in action.effects if e.kind == "set_tier_draft").data
    assert draft["tiers"] == [[1, 19000]]


def test_rep_announcing_a_rule_gets_its_question_before_the_price() -> None:
    action = _decide(
        "Before that, let me tell you the payment levels.",
        NegotiationState(phase=Phase.DISCOVERY, optional_sweep_done=True),
    )
    assert action.intent == Intent.ASK and action.reason == "max_segments"
    spoken = " ".join(render_action(action, _REF))
    assert spoken.startswith("Go ahead. How many distinct payment levels")


def test_sweep_asks_once_and_only_for_a_flexible_schedule() -> None:
    neg = NegotiationState(phase=Phase.DISCOVERY)
    assert _decide("Okay.", neg).reason == "optional_sweep"
    assert _decide("Okay.", neg, belief=_belief("even")).intent == Intent.ASK_SETTLEMENT
    assert _decide("Okay.", neg, belief=_belief("balloon")).intent == Intent.ASK_SETTLEMENT
    done = NegotiationState(phase=Phase.DISCOVERY, optional_sweep_done=True)
    assert _decide("Okay.", done).intent == Intent.ASK_SETTLEMENT
    # A bare yes to the sweep means "there are tiers".
    after = NegotiationState(
        phase=Phase.DISCOVERY, optional_sweep_done=True, last_question_key="ASK:optional_sweep"
    )
    yes = decide(_belief(), after, acts_turn(stance="accept"), None, settings=_SETTINGS)
    assert yes.intent == Intent.ASK and yes.reason == "min_payment_tiers"


def test_loop_guard_rewords_a_question_the_rep_did_not_answer() -> None:
    neg = NegotiationState(
        phase=Phase.DISCOVERY,
        optional_sweep_done=True,
        last_question_key="ASK_SETTLEMENT:",
        question_counts={"ASK_SETTLEMENT:": 1},
        no_progress_turns=1,
    )
    action = _decide("Hold on a second.", neg)
    assert action.intent == Intent.ASK_SETTLEMENT
    spoken = " ".join(render_action(action, _REF))
    assert spoken != TEMPLATES[Intent.ASK_SETTLEMENT] and "percentage" in spoken
    assert any(e.kind == "note_question" for e in action.effects)


def test_corpus_rule_announcements_are_read_in_code() -> None:
    """With no model claim, the cues alone label every ``defines`` corpus line."""
    lines = [ln for ln in load_corpus() if {"defines", "defines_neg"} & set(ln["tags"])]
    assert len(lines) >= 10
    for ln in lines:
        got = repair_defines_field(None, None, ln["text"], given_fields=set(ln.get("terms", {})))
        assert got == (ln.get("defines_field"), ln.get("expected_count")), ln["id"]


async def test_tier_definer_autoplay_reaches_a_deal_on_the_tiers() -> None:
    sid = "tier_definer"
    call = load_scenario(resolve_scenario_dir(sid), rebase_to=date(2026, 10, 9))
    orch, creditor = new_autoplay_call(call, sid, settings=_SETTINGS, audit=None)
    moves: list[tuple[str, str | None]] = []

    async def on_agent(utt) -> None:  # noqa: ANN001
        moves.append((utt.action.intent.value, utt.action.reason))

    async def on_creditor(_text: str) -> None:
        return None

    result = await run_autoplay(orch, creditor, on_agent=on_agent, on_creditor=on_creditor)
    truth = json.loads((resolve_scenario_dir(sid) / "sim.json").read_text())
    tiers = orch.session.belief.get("min_payment_tiers")
    assert [list(t) for t in tiers.value] == truth["rules"]["min_payment_tiers"]
    assert tiers.status == TermStatus.KNOWN
    # Every tier is asked for before the first price question.
    first_price = moves.index(("ASK_SETTLEMENT", None))
    assert moves[:first_price].count(("ASK", "min_payment_tiers")) == 4
    assert moves[first_price - 1] == ("READ_BACK", "min_payment_tiers")
    meta = json.loads((resolve_scenario_dir(sid) / "meta.json").read_text())
    assert result.outcome == "deal" == meta["expected"]
    assert orch.session.agreement is not None
    assert orch.session.agreement.bp >= truth["floor_bp"]


def test_tier_definer_card_matches_its_autoplay_truth() -> None:
    """The suggested tier lines (built from ``sim.json``, Phase 69) give the sim rep's tiers."""
    folder = resolve_scenario_dir("tier_definer")
    card = (folder / "rep_card.md").read_text(encoding="utf-8")
    truth = json.loads((folder / "sim.json").read_text())
    lines = merge_suggestions(rep_card_suggestions(card), truth["rules"])
    at = lines.index("Before we get to a price, I need to define the payment tiers.")
    assert lines[at + 1 : at + 7] == [
        "There are three payment tiers.",
        "The first two payments must be at least one hundred ninety dollars.",
        "Payments three to five must be at least two hundred fifty dollars.",
        "Payments six to eight must be at least three hundred dollars.",
        "That's all the tiers.",
        "At most three payment levels.",
    ]
    assert rule_reply_lines({"max_segments": 2, "min_payment_tiers": []}) == []


def test_a_count_of_payment_levels_is_the_limit_not_an_announcement() -> None:
    # Haiku (2026-11-07 corpus t06) read this as tiers = 2 plus "levels to come".
    claimed = acts_turn(
        stance="info",
        terms=[
            rule_act(field="min_payment_tiers", value=2, quote="At most two payment levels")
        ],
        defines_field="max_segments",
        expected_count=2,
    )
    out = verify_turn(claimed, "At most two payment levels.", ref=_REF)
    assert [(t.field, t.value, t.verified) for t in out.terms] == [("max_segments", 2, True)]
    assert flag(out, "defines_field") is None and flag(out, "expected_count") is None
    # Tiers keep their meaning: "three tiers" is a list still to come.
    tiers = verify_turn(
        acts_turn(stance="info"), "So we will have three payment tiers.", ref=_REF
    )
    assert flag(tiers, "defines_field") == "min_payment_tiers"
    assert flag(tiers, "expected_count") == 3
