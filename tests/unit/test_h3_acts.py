"""Phase 24b (H3): ack / answer acts, NLU question flags, 3-turn NLG context.

Policy still decides every move: with ``nlg_h3`` on, the same scenarios produce
the same intents, reasons and facts; only extra leading sentences appear.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.acts import (
    ANSWER_POINTS,
    NO_ACK_INTENTS,
    ack_facts,
    answer_act,
    attach_acts,
)
from app.agent.guards import rendered_guard, template_guard
from app.agent.nlg import (
    ACK_TEMPLATES,
    SAFE_FALLBACK,
    TEMPLATES,
    render_acts,
    speak_action,
)
from app.agent.nlg_bank import load_bank
from app.agent.nlu import VerifiedAnalysis, coerce_analysis_payload, post_verify, repair_question
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.domain.actions import Action, AnswerAct, Intent, Phase
from app.domain.belief import BeliefChange, TermStatus
from app.domain.facts import Fact
from app.domain.nlu_types import QUESTION_TOPICS
from app.domain.scenario import load_scenario
from app.domain.units import render_money, render_pct
from app.llm.client import FakeLLM
from app.llm.prompts import NLG_CONTEXT_TURNS, nlg_messages, nlu_messages
from app.store.audit import AuditLog
from eval.run_eval import run_one_scenario
from tests.seed7 import slot
from tests.wsutil import offline_settings

_REF = date(2026, 3, 1)


def _known(field: str, value: Any, *, old: Any = None, old_status: str = "UNKNOWN") -> BeliefChange:
    return BeliefChange(
        field=field,
        old_value=old,
        new_value=value,
        old_status=TermStatus(old_status),
        new_status=TermStatus.KNOWN,
        turn=1,
    )


def _ask() -> Action:
    return Action(intent=Intent.ASK_SETTLEMENT, next_phase=Phase.NEGOTIATE)


# ----- ack facts -----


def test_ack_facts_only_creditor_said_known_values() -> None:
    changes = [
        _known("max_payments", 6),
        _known("min_payment_cents", 25_000),
        _known("first_payment_date", date(2026, 4, 1)),
        _known("payment_structure", "even"),  # not a numeric ack field
    ]
    said = {("count", 6), ("money", 25_000), ("date", date(2026, 4, 1))}
    facts = ack_facts(changes, said, set())
    assert list(facts) == ["ack_max_payments", "ack_min_payment", "ack_first_payment_date"]
    assert all(f.visibility == "PUBLIC" and f.source == "creditor" for f in facts.values())
    assert facts["ack_min_payment"].kind == "money" and facts["ack_min_payment"].value == 25_000


def test_ack_skips_unsaid_private_tentative_and_unchanged() -> None:
    said = {("count", 6), ("money", 25_000)}
    # Not said by the creditor (e.g. resolved from a cents clarify): no echo.
    assert ack_facts([_known("max_payments", 7)], said, set()) == {}
    # Collides with a private client figure: never echoed.
    assert ack_facts([_known("min_payment_cents", 25_000)], said, {("money", 25_000)}) == {}
    # Hedged → TENTATIVE (read back instead).
    tentative = _known("max_payments", 6).model_copy(update={"new_status": TermStatus.TENTATIVE})
    assert ack_facts([tentative], said, set()) == {}
    # Restated, unchanged value: no repeat ack.
    same = _known("max_payments", 6, old=6, old_status="KNOWN")
    assert ack_facts([same], said, set()) == {}
    # Changed value: acked.
    changed = _known("max_payments", 6, old=4, old_status="KNOWN")
    assert set(ack_facts([changed], said, set())) == {"ack_max_payments"}


def test_attach_acts_never_changes_the_move() -> None:
    action = Action(
        intent=Intent.COUNTER,
        facts={
            "counter_pct": Fact(
                id="counter_pct", kind="pct", value=4000, visibility="PUBLIC", source="engine"
            )
        },
        required={"counter_pct"},
        next_phase=Phase.NEGOTIATE,
        reason="bp=4000",
    )
    analysis = VerifiedAnalysis(asks_question=True, question_topic="why_not_higher")
    out = attach_acts(
        action,
        analysis,
        [_known("max_payments", 6)],
        creditor_numbers={("count", 6)},
        private_blocklist=set(),
    )
    assert set(out.ack) == {"ack_max_payments"}
    assert out.answer == AnswerAct(topic="why_not_higher", text=ANSWER_POINTS["why_not_higher"])
    for name in ("intent", "facts", "required", "effects", "next_phase", "reason", "text_slots"):
        assert getattr(out, name) == getattr(action, name), name


@pytest.mark.parametrize("intent", sorted(NO_ACK_INTENTS))
def test_no_ack_before_readback_refusals_and_endings(intent: Intent) -> None:
    action = Action(intent=intent, next_phase=Phase.NEGOTIATE)
    out = attach_acts(
        action,
        VerifiedAnalysis(),
        [_known("max_payments", 6)],
        creditor_numbers={("count", 6)},
        private_blocklist=set(),
    )
    assert out.ack == {}


def test_private_ask_is_refused_not_answered() -> None:
    analysis = VerifiedAnalysis(
        asks_question=True, question_topic="other", asks_client_private_info=True
    )
    assert answer_act(_ask(), analysis) is None
    refuse = Action(intent=Intent.REFUSE_PRIVATE, next_phase=Phase.NEGOTIATE)
    assert (
        answer_act(refuse, VerifiedAnalysis(asks_question=True, question_topic="timeline")) is None
    )
    # The NLU repair drops the question flag on a private ask too.
    assert repair_question(True, "other", "What is the client's income?", asks_private=True) == (
        False,
        None,
    )


def test_answer_points_cover_topics_and_pass_guards() -> None:
    assert set(ANSWER_POINTS) == set(QUESTION_TOPICS)
    for topic, text in ANSWER_POINTS.items():
        assert template_guard(text, set(), set()).ok, topic
        assert rendered_guard(text, {}, set(), set(), ref=_REF).ok, topic


# ----- rendering -----


def test_render_ack_and_answer_lead_the_move() -> None:
    action = _ask().model_copy(
        update={
            "ack": ack_facts(
                [_known("max_payments", 6), _known("min_payment_cents", 25_000)],
                {("count", 6), ("money", 25_000)},
                set(),
            ),
            "answer": AnswerAct(topic="next_steps", text=ANSWER_POINTS["next_steps"]),
        }
    )
    out = render_acts(action, _REF, creditor_numbers={("count", 6), ("money", 25_000)})
    assert out[0] == "Got it, 6 payments at a $250 minimum."
    assert out[1:] == [ANSWER_POINTS["next_steps"]]


def test_every_ack_template_passes_template_guard() -> None:
    for ids, template in ACK_TEMPLATES.items():
        assert template_guard(template, set(ids), set(ids)).ok, ids


def test_guard_failing_act_is_dropped_not_fallback(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.db")
    action = _ask().model_copy(
        update={"answer": AnswerAct(topic="timeline", text="We reply within 3 days.")}
    )
    blocked: list[dict[str, Any]] = []
    out = render_acts(action, _REF, audit=audit, call_id="c1", blocked_out=blocked)
    assert out == []  # dropped; never SAFE_FALLBACK for an optional act
    assert blocked and blocked[0]["reason"] == "unverified_number"
    assert any(e["type"] == "act_dropped" for e in audit.for_call("c1"))


def test_bank_has_guard_clean_ack_and_answer_variants() -> None:
    bank = load_bank()
    ack_keys = [k for k in bank if k[0] == "ACK"]
    answer_keys = [k for k in bank if k[0].startswith("ANSWER:")]
    assert {k[1] for k in ack_keys} == set(ACK_TEMPLATES)
    assert {k[0].split(":", 1)[1] for k in answer_keys} == set(QUESTION_TOPICS)
    for key in ack_keys + answer_keys:
        assert bank[key], key
        ids = set(key[1])
        for t in bank[key]:
            assert template_guard(t, ids, ids).ok, t
            plain = t
            for i in ids:
                plain = plain.replace("{" + i + "}", "that")
            assert rendered_guard(plain, {}, set(), set(), ref=_REF).ok, t
    # The policy talking point itself is always one of the answer variants.
    for topic, text in ANSWER_POINTS.items():
        assert text in bank[("ANSWER:" + topic, ())]


# ----- NLU question flags -----


@pytest.mark.parametrize(
    ("llm_flag", "llm_topic", "utterance", "expected"),
    [
        (True, "who_approves", "Who signs off on this?", (True, "who_approves")),
        (False, None, "Why can't you go any higher than that?", (True, "why_not_higher")),
        (True, "other", "So what happens next?", (True, "next_steps")),
        (False, None, "How long until we hear back from the client?", (True, "timeline")),
        (True, "other", "Is your office open on weekends?", (True, "other")),
        # A statement is never a question, whatever the LLM says.
        (True, "next_steps", "We can do six payments.", (False, None)),
        (False, None, "Can you do forty-five percent?", (False, None)),
        # Seen live: an on-script question the LLM labelled "other".
        (
            True,
            "other",
            "Could you let me know the settlement percentage for the $178.60 payment?",
            (False, None),
        ),
    ],
)
def test_repair_question(
    llm_flag: bool, llm_topic: str | None, utterance: str, expected: tuple[bool, str | None]
) -> None:
    assert repair_question(llm_flag, llm_topic, utterance, asks_private=False) == expected


def test_off_list_topic_coerced_not_rejected() -> None:
    data = coerce_analysis_payload({"asks_question": True, "question_topic": "pricing"})
    assert TurnAnalysis.model_validate(data).question_topic == "other"


def test_post_verify_carries_question_into_turn_analysis() -> None:
    verified = post_verify(
        TurnAnalysis(stance="question", asks_question=True, question_topic="who_approves"),
        "Who approves this on your side?",
        ref=_REF,
    )
    assert verified.asks_question and verified.question_topic == "who_approves"
    ta = verified.to_turn_analysis()
    assert ta.asks_question and ta.question_topic == "who_approves"


def test_nlu_prompt_asks_for_question_fields() -> None:
    system = nlu_messages("hi", "", None)[0]["content"]
    assert "asks_question" in system and "question_topic" in system
    for topic in QUESTION_TOPICS:
        assert topic in system


# ----- NLG context: last 3 public turns -----


def test_nlg_prompt_carries_last_three_turns() -> None:
    turns = [
        ("creditor", "a"),
        ("agent", "b"),
        ("creditor", "c"),
        ("agent", "d"),
        ("creditor", "e"),
    ]
    user = nlg_messages(Intent.COUNTER, ["counter_pct"], "e", recent_turns=turns)[1]["content"]
    assert NLG_CONTEXT_TURNS == 3
    assert "Rep: c\nAgent: d\nRep: e" in user
    assert "Rep: a" not in user and "Agent: b" not in user
    # Without context: the old one-line form.
    assert "Rep last said: e" in nlg_messages(Intent.COUNTER, ["counter_pct"], "e")[1]["content"]


def _orch(tmp_path: Path, llm: Any = None, db: str = "a.db", **kw: Any) -> Orchestrator:
    session = CallSession(scenario=load_scenario("fixtures/scenarios/easy_deal"))
    return Orchestrator(
        session,
        llm=llm,
        settings=offline_settings(**kw),
        audit=AuditLog(tmp_path / db),
        auto_ack=True,
    )


def test_recent_public_turns_drop_private_and_unspoken(tmp_path: Path) -> None:
    from app.agent.session import Turn

    orch = _orch(tmp_path)
    s = orch.session
    s.private_blocklist.add(("money", 31_400))
    s.history.extend(
        [
            Turn(role="creditor", text="Hello there.", spoken=True),
            Turn(role="agent", text="Never heard.", spoken=False, sentence_id="x"),
            Turn(role="agent", text="Hi, thanks.", spoken=True, sentence_id="y"),
            Turn(role="creditor", text="Is it $314 a month?", spoken=True),
            Turn(role="creditor", text="Six payments.", spoken=True),
        ]
    )
    got = orch._recent_public_turns()
    assert got == [
        ("creditor", "Hello there."),
        ("agent", "Hi, thanks."),
        ("creditor", "Six payments."),
    ]


async def test_llm_nlg_prompt_has_context_and_no_private_value(tmp_path: Path) -> None:
    """Live-NLG path: the prompt sees 3 public turns and never a PRIVATE figure."""
    llm = FakeLLM()
    prompts: list[str] = []
    real = llm.chat_text

    async def spy(role: str, messages: list[dict[str, Any]], max_tokens: int) -> str:
        if role == "nlg":
            prompts.append(json.dumps(messages))
        return await real(role, messages, max_tokens)

    llm.chat_text = spy  # type: ignore[method-assign]
    for _ in range(6):
        llm.enqueue("nlg", "")  # empty template → falls back to TEMPLATES (21.7)
    orch = _orch(tmp_path, llm=llm, nlg_mode="llm", nlg_h3=True)
    await orch.start()
    await orch.on_creditor_text(
        "Max eight payments, minimum one hundred dollars, even payments please.",
        oracle=TurnAnalysis(
            stance="info",
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="eight"),
                ExtractedTerm(field="min_payment_cents", value=10000, quote="one hundred dollars"),
                ExtractedTerm(field="payment_structure", value="even", quote="even"),
            ],
        ),
    )
    utt = await orch.on_creditor_text(
        "We are looking for a forty five percent settlement.",
        oracle=TurnAnalysis(
            stance="offer", settlement_ask_pct=45.0, ask_quote="forty five percent"
        ),
    )
    assert prompts, "the counter / confirm turn should call the nlg role"
    last = prompts[-1]
    assert "Recent conversation" in last and "Max eight payments" in last
    private = orch.session.private_blocklist
    assert private, "the session should have a private blocklist by now"
    for kind, value in private:
        if kind == "money" and isinstance(value, int):
            assert render_money(value) not in last, value
        if kind == "pct" and isinstance(value, int):
            assert render_pct(value) not in last, value
    # Empty LLM template → default template, not SAFE_FALLBACK.
    assert SAFE_FALLBACK not in [t for _, t in utt.sentences]


async def test_empty_llm_template_falls_back_to_default_template() -> None:
    llm = FakeLLM()
    llm.enqueue("nlg", "")
    llm.enqueue("nlg", "   ")
    action = Action(
        intent=Intent.REFUSE_COMMIT, next_phase=Phase.NEGOTIATE
    )  # no required ids: an empty template would pass template_guard
    trace: dict[str, Any] = {}
    out = await speak_action(
        action,
        _REF,
        llm=llm,
        settings=offline_settings(nlg_mode="llm"),
        trace_out=trace,
    )
    assert out == [TEMPLATES[Intent.REFUSE_COMMIT]]
    assert trace["fallback_reason"] == "llm_template_rejected"


# ----- orchestrator: moves identical, acts spoken first -----


async def test_h3_turn_speaks_ack_then_move(tmp_path: Path) -> None:
    orch = _orch(tmp_path, nlg_h3=True)
    await orch.start()
    utt = await orch.on_creditor_text(
        "Max eight payments, minimum one hundred dollars, even payments please.",
        oracle=TurnAnalysis(
            stance="info",
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="eight"),
                ExtractedTerm(field="min_payment_cents", value=10000, quote="one hundred dollars"),
                ExtractedTerm(field="payment_structure", value="even", quote="even"),
            ],
        ),
    )
    lines = [t for _, t in utt.sentences]
    assert utt.action.intent == Intent.ASK_SETTLEMENT
    assert lines[0] == "Got it, 8 payments at a $100 minimum."
    assert lines[-1] == TEMPLATES[Intent.ASK_SETTLEMENT]
    decide = [e for e in orch.audit.for_call(orch.session.call_id) if e["type"] == "decide"]  # type: ignore[union-attr]
    assert decide[-1]["payload"]["acts"] == {
        "ack": ["ack_max_payments", "ack_min_payment"],
        "answer": None,
    }


async def test_h3_question_attaches_answer_without_replacing_move(tmp_path: Path) -> None:
    plain = _orch(tmp_path, nlg_h3=False, db="plain.db")
    h3 = _orch(tmp_path, nlg_h3=True, db="h3.db")
    for o in (plain, h3):
        await o.start()
    text = "Before terms, who approves this on your side?"
    oracle = TurnAnalysis(stance="question", asks_question=True, question_topic="who_approves")
    a = await plain.on_creditor_text(text, oracle=oracle)
    b = await h3.on_creditor_text(text, oracle=oracle)
    assert a.action.intent == b.action.intent and a.action.reason == b.action.reason
    assert b.action.answer is not None and b.action.answer.topic == "who_approves"
    b_lines = [t for _, t in b.sentences]
    assert b_lines[0] == ANSWER_POINTS["who_approves"]
    assert b_lines[1:] == [t for _, t in a.sentences]


async def test_h3_moves_identical_to_plain_policy_on_seed7(tmp_path: Path) -> None:
    """Same intents / reasons / outcome with acts on; only extra lines are spoken."""
    llm = FakeLLM()
    plain_cfg = offline_settings(max_turns=24)
    h3_cfg = offline_settings(max_turns=24, nlg_h3=True)
    acked = 0
    (tmp_path / "h3").mkdir()
    for i in (1, 2, 35, 68, 75):
        sc = slot(i)
        a = await run_one_scenario(
            sc, settings=plain_cfg, llm=llm, sim_phrasing="template", audit_dir=tmp_path
        )
        b = await run_one_scenario(
            sc,
            settings=h3_cfg,
            llm=llm,
            sim_phrasing="template",
            audit_dir=tmp_path / "h3",
            agent="policy_h3",
        )
        assert a["intents"] == b["intents"], sc.id
        for key in ("final_reason", "agreed_bp", "agreement_valid", "phase", "sensitive_leaks"):
            assert a[key] == b[key], (sc.id, key)
        assert b["unverified_figures_spoken"] == 0 and b["sensitive_leaks"] == 0
        assert len(b["agent_lines"]) >= len(a["agent_lines"])
        acked += sum(1 for line in b["agent_lines"] if line.startswith("Got it"))
    assert acked > 0
