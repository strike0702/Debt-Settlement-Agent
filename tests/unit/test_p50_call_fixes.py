"""Phase 50: the user's manual call, replayed offline, and the four fixes it led to.

- Equal steps: a rep who does not move gets a hold, then two equal steps to
  min(their ask, accept line), then a handoff (or an accept at or below the line).
- No objection to a term the rep never gave: an ASSUMED start date is proposed
  as a plain question, not "That start date does not fit".
- "We can be flexible on other terms" is not a flexible payment structure.
- The payment structure is acknowledged ("Got it, a balloon schedule."), and a
  one-payment plan is spoken "1 payment".

``replay_manual_call`` is also what the PROGRESS before / after lines came from
(oracle NLU standing in for Haiku, template NLG, no LLM call).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.acts import STRUCTURE_ACK_PHRASES, ack_facts, acked_fields
from app.agent.nlg import ack_template, ack_variants, render_action
from app.agent.nlu import post_verify, structure_word_ok
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.policy import NegotiationState, decide
from app.agent.session import CallSession
from app.domain.actions import Action, Intent, Phase
from app.domain.belief import BeliefChange, TermStatus
from app.domain.facts import Fact
from app.domain.scenario import load_scenario
from app.store.audit import AuditLog
from tests.unit.test_policy import _afford, _belief
from tests.wsutil import offline_settings

_REF = date(2026, 10, 9)
_STRUCTURE_Q = "Do you need even payments, or can you allow a balloon or a flexible schedule?"


def _t(field: str, value: Any, quote: str) -> ExtractedTerm:
    return ExtractedTerm(field=field, value=value, quote=quote, hedged=False)


# The rep's side of the user's call (live NLU read as below; Haiku's reading of
# line 5 is the bug: "flexible" as a payment structure).
_SCRIPT: list[tuple[str, TurnAnalysis]] = [
    ("4", TurnAnalysis(stance="info", terms=[_t("max_payments", 4, "4")])),
    ("100$", TurnAnalysis(stance="info", terms=[_t("min_payment_cents", 10000, "100$")])),
    (
        "balloon works",
        TurnAnalysis(stance="info", terms=[_t("payment_structure", "balloon", "balloon")]),
    ),
    ("80%", TurnAnalysis(stance="offer", settlement_ask_pct=80.0, ask_quote="80%")),
    (
        "no, we can be flexible on other terms though",
        TurnAnalysis(stance="reject", terms=[_t("payment_structure", "flexible", "flexible")]),
    ),
    ("balloon", TurnAnalysis(stance="info", terms=[_t("payment_structure", "balloon", "balloon")])),
    ("okay", TurnAnalysis(stance="accept")),
    ("80%", TurnAnalysis(stance="offer", settlement_ask_pct=80.0, ask_quote="80%")),
    ("no", TurnAnalysis(stance="reject")),
    ("sorry", TurnAnalysis(stance="other")),
    ("no doesn't work", TurnAnalysis(stance="reject")),
]


async def replay_manual_call(tmp_path: Path) -> list[tuple[str, str]]:
    """(speaker, line) for the call; "balloon" is said only when the agent asked which."""
    session = CallSession(scenario=load_scenario("fixtures/scenarios/no_space"))
    orch = Orchestrator(
        session,
        llm=None,
        settings=offline_settings(max_counters=6),
        audit=AuditLog(tmp_path / "p50.db"),
        auto_ack=True,
    )
    out: list[tuple[str, str]] = []
    first = await orch.start()
    out.append(("AGENT", " ".join(t for _, t in first.sentences)))
    last: Intent | None = None
    for text, analysis in _SCRIPT:
        if text == "balloon" and last != Intent.CLARIFY:
            continue
        out.append(("REP", text))
        utt = await orch.on_creditor_text(text, oracle=analysis)
        out.append(("AGENT", " ".join(t for _, t in utt.sentences)))
        last = utt.action.intent
        if last in (Intent.ESCALATE, Intent.CONFIRM_SCHEDULE, Intent.PROPOSE_WRAP):
            break
    return out


async def test_manual_call_replay_after_phase_50(tmp_path: Path) -> None:
    lines = await replay_manual_call(tmp_path)
    agent = [t for who, t in lines if who == "AGENT"]
    said = " ".join(agent)
    # 2: the rep never gave a start date, so no "does not fit" objection.
    assert "That start date does not fit" not in said
    assert any(t.startswith("Could payment start on ") for t in agent)
    # 3: "flexible on other terms" set nothing, so no "which is it" clarify.
    assert "Which is it" not in said
    # 4: the structure is acknowledged.
    assert "a balloon schedule" in said
    # 1: hold, then two equal steps to the 66% line, then the handoff.
    pcts = [t.split("%")[0].rsplit(" ", 1)[-1] for t in agent if "% of the balance" in t]
    assert pcts == ["46", "46", "56", "66"]
    assert "Your number is above what I can accept on this call" in agent[-1]


# ----- 1. equal steps -----


def _ladder(ask: int, afford: Any, *, stages: int = 4) -> list[tuple[str, int | None]]:
    """Walk a rep who never moves from ``ask``; (reason, bp) per agent move."""
    b = _belief(max_payments=4, min_payment_cents=10000, payment_structure="balloon")
    neg = NegotiationState(ask_bp=ask, phase=Phase.NEGOTIATE, turn_idx=5)
    settings = offline_settings(max_counters=6)
    moves: list[tuple[str, int | None]] = []
    analysis = TurnAnalysis(stance="offer", settlement_ask_pct=ask / 100)
    for _ in range(stages):
        a = decide(b, neg, analysis, afford, settings=settings)
        bp = a.facts["counter_pct"].value if "counter_pct" in a.facts else None
        if "settlement_pct" in a.facts:
            bp = a.facts["settlement_pct"].value
        moves.append((a.reason or "", bp if isinstance(bp, int) else None))
        if a.intent != Intent.COUNTER:
            break
        data = next(e for e in a.effects if e.kind == "offer_counter").data
        assert isinstance(bp, int)
        neg.counters_offered.append(bp)
        neg.counter_turns.append(neg.turn_idx)
        neg.ask_at_last_counter = data["ask_bp"]
        neg.hold_stage = data["stage"]
        neg.turn_idx += 1
        analysis = TurnAnalysis(stance="reject")
    return moves


def test_equal_steps_from_the_manual_call() -> None:
    """Ceiling 88% → line 66%; anchor 46%, hold, 56%, 66%, then hand off (ask 80%)."""
    afford = _afford(8800, list(range(100, 8900, 100)))
    moves = _ladder(8000, afford, stages=5)
    assert moves == [
        ("bp=4600", 4600),
        ("hold", 4600),
        ("step", 5600),
        ("step", 6600),
        ("above_accept_line", None),
    ]


def test_equal_steps_land_on_their_ask_when_it_is_below_the_line() -> None:
    afford = _afford(8800, list(range(100, 8900, 100)))  # line 6600
    moves = _ladder(6000, afford, stages=5)
    # anchor 0.7 × 60 = 42; half of (60 − 42) = 9 → 51; then exactly 60; then accept it.
    assert [m for m in moves] == [
        ("bp=4200", 4200),
        ("hold", 4200),
        ("step", 5100),
        ("step", 6000),
        ("ask_within_offer", 6000),
    ]


def test_equal_steps_snap_to_the_schedulable_grid_and_never_pass_the_line() -> None:
    grid = [bp for bp in range(100, 8900, 100) if bp % 300 == 0]  # every 3 points
    afford = _afford(8800, grid)
    moves = _ladder(8000, afford, stages=5)
    bps = [bp for _, bp in moves if bp is not None]
    assert all(bp <= 6600 for bp in bps)
    assert moves[-2] == ("step", 6600) and moves[-1] == ("above_accept_line", None)


# ----- 2. no objection to an assumed term -----


def _alt_move(status: TermStatus) -> Action:
    b = _belief(max_payments=4, min_payment_cents=10000, payment_structure="balloon")
    if status != TermStatus.ASSUMED:
        b.observe(
            "first_payment_date", date(2026, 11, 1), "November 1", 1, verified=True, hedged=False
        )
        assert b.get("first_payment_date").status == status
    else:
        assert b.get("first_payment_date").status == TermStatus.ASSUMED
    return decide(
        b,
        NegotiationState(ask_bp=8000, phase=Phase.NEGOTIATE, turn_idx=5),
        TurnAnalysis(stance="offer", settlement_ask_pct=80.0),
        _afford(None, []),
        settings=offline_settings(),
        term_alt=("first_payment_date", date(2026, 12, 31)),
    )


def test_assumed_start_date_is_proposed_as_a_question() -> None:
    action = _alt_move(TermStatus.ASSUMED)
    assert action.intent == Intent.COUNTER_TERMS
    line = " ".join(render_action(action, _REF))
    assert line == "Could payment start on December 31?"
    assert "does not fit" not in line


def test_stated_start_date_keeps_the_objection() -> None:
    action = _alt_move(TermStatus.KNOWN)
    line = " ".join(render_action(action, _REF))
    assert line.startswith("That start date does not fit the client's program.")


@pytest.mark.parametrize(
    ("field", "value", "said", "assumed"),
    [
        (
            "min_payment_cents",
            5000,
            "These terms do not fit",
            "Could you allow a minimum payment of $50?",
        ),
        ("max_payments", 6, "These terms do not fit", "Could you allow up to 6 payments?"),
    ],
)
def test_other_fields_only_object_when_the_rep_said_them(
    field: str, value: int, said: str, assumed: str
) -> None:
    from app.agent.policy import _counter_terms_action

    stated = _counter_terms_action(field=field, value=value, effects=[])
    quiet = _counter_terms_action(field=field, value=value, effects=[], rep_stated=False)
    assert " ".join(render_action(stated, _REF)).startswith(said)
    assert " ".join(render_action(quiet, _REF)) == assumed


# ----- 3. structure words need a payment cue -----


@pytest.mark.parametrize(
    ("value", "line", "last", "ok"),
    [
        # the manual call and other "flexible about something else" near-misses
        ("flexible", "no, we can be flexible on other terms though", "", False),
        ("flexible", "no, we can be flexible on other terms though", _STRUCTURE_Q, False),
        ("flexible", "We are flexible on the start date.", _STRUCTURE_Q, False),
        ("flexible", "We're flexible about timing.", "", False),
        ("flexible", "We are flexible.", "", False),
        ("flexible", "We can be flexible with you here.", "", False),
        ("even", "We can't even do that.", "", False),
        ("even", "Even then, eighty is the number.", "", False),
        # real structure statements
        ("flexible", "Flexible payments are fine.", "", True),
        ("flexible", "We can do a flexible schedule.", "", True),
        ("flexible", "The payments can be flexible.", "", True),
        ("flexible", "flexible, the payments can vary", "", True),
        ("flexible", "We are flexible.", _STRUCTURE_Q, True),
        ("flexible", "We're flexible on the payments.", "", True),
        ("even", "We need even payments.", "", True),
        ("even", "Even is fine.", _STRUCTURE_Q, True),
        ("balloon", "balloon works", _STRUCTURE_Q, True),
        ("balloon", "Five payments, minimum one hundred dollars, balloon is fine.", "", True),
        ("balloon", "A balloon at the end works.", "", True),
    ],
)
def test_structure_word_needs_a_payment_cue(value: str, line: str, last: str, ok: bool) -> None:
    assert structure_word_ok(value, line, last_agent_line=last) is ok


def test_post_verify_drops_flexible_terms_as_a_structure() -> None:
    analysis = TurnAnalysis(
        stance="reject", terms=[_t("payment_structure", "flexible", "flexible")]
    )
    out = post_verify(analysis, "no, we can be flexible on other terms though", ref=_REF)
    assert out.terms == []
    assert [(d.field, d.reason) for d in out.dropped] == [
        ("payment_structure", "rejected_structure")
    ]
    kept = post_verify(
        TurnAnalysis(stance="info", terms=[_t("payment_structure", "balloon", "balloon")]),
        "balloon works",
        ref=_REF,
        last_agent_line=_STRUCTURE_Q,
    )
    assert [(t.value, t.verified) for t in kept.terms] == [("balloon", True)]


# ----- 4. structure ack and "1 payment" -----


def _change(field: str, value: Any) -> BeliefChange:
    return BeliefChange(
        field=field,
        old_value=None,
        new_value=value,
        old_status=TermStatus.UNKNOWN,
        new_status=TermStatus.KNOWN,
        turn=1,
    )


@pytest.mark.parametrize(("value", "phrase"), sorted(STRUCTURE_ACK_PHRASES.items()))
def test_structure_ack_is_a_digit_free_public_text_fact(value: str, phrase: str) -> None:
    facts = ack_facts([_change("payment_structure", value)], set(), set())
    fact = facts["ack_payment_structure"]
    assert (fact.kind, fact.value, fact.visibility, fact.source) == (
        "text",
        phrase,
        "PUBLIC",
        "creditor",
    )
    assert acked_fields(facts) == {"payment_structure": value}
    assert not any(ch.isdigit() for ch in phrase)


def test_structure_ack_variants_and_rotation() -> None:
    ids = ("ack_payment_structure",)
    assert ack_template(ids, 0) == "Got it, {ack_payment_structure}."
    assert ack_template(ids, 1) == "Understood, {ack_payment_structure} it is."
    assert len(set(ack_variants(ids))) == 6
    combo = ("ack_max_payments", "ack_payment_structure")
    assert (
        ack_template(combo, 0)
        == "Got it, up to {ack_max_payments} payments, with {ack_payment_structure}."
    )


def test_unknown_structure_value_is_not_acked() -> None:
    assert ack_facts([_change("payment_structure", "weekly")], set(), set()) == {}


def test_text_fact_rejects_digits_and_numbers_reject_strings() -> None:
    with pytest.raises(ValueError):
        Fact(id="x", kind="text", value="3 payments", visibility="PUBLIC", source="creditor")
    with pytest.raises(ValueError):
        Fact(id="x", kind="count", value="three", visibility="PUBLIC", source="creditor")


def test_structure_ack_is_spoken_before_the_move(tmp_path: Path) -> None:
    import asyncio

    session = CallSession(scenario=load_scenario("fixtures/scenarios/no_space"))
    orch = Orchestrator(
        session,
        llm=None,
        settings=offline_settings(),
        audit=AuditLog(tmp_path / "a.db"),
        auto_ack=True,
    )

    async def run() -> list[str]:
        await orch.start()
        await orch.on_creditor_text("4", oracle=_SCRIPT[0][1])
        await orch.on_creditor_text("100$", oracle=_SCRIPT[1][1])
        utt = await orch.on_creditor_text("balloon works", oracle=_SCRIPT[2][1])
        return [t for _, t in utt.sentences]

    lines = asyncio.run(run())
    assert lines[0] in (
        "Got it, a balloon schedule.",
        "Understood, a balloon schedule it is.",
        "Okay, a balloon schedule.",
        "Got it, a balloon schedule it is.",
        "Understood, a balloon schedule.",
        "Okay, a balloon schedule it is.",
    )


def test_one_payment_plan_is_spoken_singular() -> None:
    action = Action(
        intent=Intent.CONFIRM_SCHEDULE,
        facts={
            "settlement_pct": Fact(
                id="settlement_pct", kind="pct", value=4000, visibility="PUBLIC", source="engine"
            ),
            "num_payments": Fact(
                id="num_payments", kind="count", value=1, visibility="PUBLIC", source="engine"
            ),
            "offer_total": Fact(
                id="offer_total", kind="money", value=50000, visibility="PUBLIC", source="engine"
            ),
            "first_payment_date": Fact(
                id="first_payment_date",
                kind="date",
                value=date(2026, 12, 31),
                visibility="PUBLIC",
                source="engine",
            ),
        },
        next_phase=Phase.CONFIRM,
    )
    line = " ".join(render_action(action, _REF))
    assert "1 payment totaling $500" in line and "1 payments" not in line
    bank_like = action.model_copy(
        update={
            "template_override": "We could set {num_payments} payments totalling {offer_total}."
        }
    )
    assert " ".join(render_action(bank_like, _REF)) == "We could set 1 payment totalling $500."


def test_paraphrased_structure_stays_unverified_and_is_read_back() -> None:
    """No structure word in the line: the rule does not apply (pre-Phase 50 behaviour)."""
    out = post_verify(
        TurnAnalysis(stance="info", terms=[_t("payment_structure", "even", "equal installments")]),
        "We only do equal installments on these accounts.",
        ref=_REF,
    )
    assert [(t.value, t.verified) for t in out.terms] == [("even", False)]
    assert out.dropped == []
