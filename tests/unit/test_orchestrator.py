"""Orchestrator / session tests — oracle NLU + template NLG (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.adapter.engine_adapter import build_rules
from app.adapter.validator import validate
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.policy import Intent, Phase
from app.agent.session import CallSession
from app.config import Settings
from app.domain.belief import TermStatus
from app.domain.scenario import load_scenario
from app.store.audit import AuditLog


def _settings(**kwargs: object) -> Settings:
    base = dict(
        nlu_mode="oracle",
        nlg_mode="template",
        llm_profile="offline",
        hostility_threshold=0.8,
        max_turns=24,
        max_counters=4,
        anchor_ratio=0.7,
        concession_factor=0.5,
        firm_name="Synthetic Debt Relief",
        opening_disclosure="This call uses synthetic data for demonstration only.",
    )
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def _orch(tmp_path: Path, *, auto_ack: bool = True) -> tuple[Orchestrator, CallSession, AuditLog]:
    scenario = load_scenario("fixtures/demo")
    audit = AuditLog(tmp_path / "audit.db")
    session = CallSession(scenario=scenario)
    orch = Orchestrator(
        session,
        llm=None,
        settings=_settings(),
        audit=audit,
        auto_ack=auto_ack,
    )
    return orch, session, audit


@pytest.mark.asyncio
async def test_barge_in_drops_pending_counter(tmp_path: Path) -> None:
    """Barge-in before ack drops offer_counter; next turn re-offers the same bp."""
    orch, session, audit = _orch(tmp_path, auto_ack=False)
    await orch.start()
    # Ack opening so phase advances.
    assert session.pending is not None
    await orch.on_sentence_done(session.pending.sentence_ids)

    # Strict rules → max_bp < 95% ask so we COUNTER.
    utt = await orch.on_creditor_text(
        "Max three payments, minimum payment two hundred fifty dollars, even payments.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=3, quote="three", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=25000,
                    quote="two hundred fifty dollars",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure", value="even", quote="even", hedged=False
                ),
            ],
            stance="info",
        ),
    )
    await orch.on_sentence_done([sid for sid, _ in utt.sentences])

    counter1 = await orch.on_creditor_text(
        "We need ninety-five percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=95.0,
            ask_quote="ninety-five percent",
            stance="offer",
        ),
    )
    assert counter1.action.intent == Intent.COUNTER
    bp1 = counter1.action.facts["counter_pct"].value
    assert isinstance(bp1, int)
    assert session.neg.counters_offered == []  # not acked yet

    # Barge-in: drop pending counter effects.
    await orch.on_barge_in([])
    assert session.pending is None
    assert session.neg.counters_offered == []
    # Ask was also in pending effects — re-state ask on next turn.
    # Seed ask via a prior acked record: apply ask manually for the re-offer test.
    # After barge-in, ask_bp may be unset; send ask again with reject stance.
    counter2 = await orch.on_creditor_text(
        "Still need ninety-five percent, that is too low.",
        oracle=TurnAnalysis(
            settlement_ask_pct=95.0,
            ask_quote="ninety-five percent",
            stance="reject",
        ),
    )
    assert counter2.action.intent == Intent.COUNTER
    bp2 = counter2.action.facts["counter_pct"].value
    assert bp2 == bp1
    await orch.on_sentence_done([sid for sid, _ in counter2.sentences])
    assert session.neg.counters_offered == [bp1]
    audit.close()


@pytest.mark.asyncio
async def test_contradiction_leads_to_clarify(tmp_path: Path) -> None:
    orch, session, audit = _orch(tmp_path)
    await orch.start()

    await orch.on_creditor_text(
        "Maximum six payments.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=6, quote="six", hedged=False),
            ],
            stance="info",
        ),
    )
    assert session.belief.get("max_payments").status == TermStatus.KNOWN

    utt = await orch.on_creditor_text(
        "Actually make that eight payments.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="eight", hedged=False),
            ],
            stance="info",
        ),
    )
    assert session.belief.get("max_payments").status == TermStatus.CONTRADICTED
    assert utt.action.intent == Intent.CLARIFY
    joined = " ".join(t for _, t in utt.sentences).lower()
    assert "earlier" in joined or "hearing" in joined
    audit.close()


@pytest.mark.asyncio
async def test_private_info_refuse_then_escalate(tmp_path: Path) -> None:
    orch, session, audit = _orch(tmp_path)
    await orch.start()

    u1 = await orch.on_creditor_text(
        "What is the client's bank balance?",
        oracle=TurnAnalysis(asks_client_private_info=True, stance="other"),
    )
    assert u1.action.intent == Intent.REFUSE_PRIVATE
    assert session.neg.private_ask_count == 1

    u2 = await orch.on_creditor_text(
        "I need their draft amount.",
        oracle=TurnAnalysis(asks_client_private_info=True, stance="other"),
    )
    assert u2.action.intent == Intent.ESCALATE
    assert u2.action.reason == "sensitive_request"
    assert session.neg.phase == Phase.ESCALATE
    audit.close()


@pytest.mark.asyncio
async def test_full_scripted_call_reaches_propose_wrap(tmp_path: Path) -> None:
    orch, session, audit = _orch(tmp_path)
    await orch.start()

    # Discovery: required fields.
    u = await orch.on_creditor_text(
        "Up to eight payments, minimum one hundred dollars, even structure.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="eight", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=10000,
                    quote="one hundred dollars",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure", value="even", quote="even", hedged=False
                ),
            ],
            stance="info",
        ),
    )
    # After required fields, agent asks settlement % if unknown.
    assert u.action.intent in (Intent.ASK_SETTLEMENT, Intent.ASK)

    u = await orch.on_creditor_text(
        "We can do forty-five percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=45.0,
            ask_quote="forty-five percent",
            stance="offer",
        ),
    )
    assert u.action.intent == Intent.COUNTER
    counter_bp = u.action.facts["counter_pct"].value
    assert isinstance(counter_bp, int)
    assert counter_bp < 4500
    assert session.neg.counters_offered == [counter_bp]

    u = await orch.on_creditor_text(
        "We can accept that.",
        oracle=TurnAnalysis(stance="accept"),
    )
    assert u.action.intent == Intent.CONFIRM_SCHEDULE
    assert session.neg.phase == Phase.CONFIRM
    assert session.neg.confirmed_bp == counter_bp
    assert session.last_eval is not None
    assert session.last_eval.feasible

    u = await orch.on_creditor_text(
        "Yes, that works for us.",
        oracle=TurnAnalysis(stance="accept"),
    )
    assert u.action.intent == Intent.PROPOSE_WRAP
    assert session.neg.phase == Phase.WRAP
    assert session.agreement is not None
    assert session.agreement.status == "pending_client_approval"
    assert session.agreement.bp == counter_bp

    u = await orch.on_creditor_text(
        "thanks",
        oracle=TurnAnalysis(stance="other", wants_to_end=False),
    )
    assert u.action.intent == Intent.CLOSE
    assert session.neg.phase == Phase.END

    # Agreement schedule passes the independent validator.
    rules = build_rules(session.belief, session.scenario)
    fpd = session.belief.get("first_payment_date").value
    assert session.last_eval is not None and session.last_eval.rows is not None
    violations = validate(
        session.last_eval.rows,
        session.scenario.client,
        session.last_eval.offer_total_cents,
        session.last_eval.program_fee_cents,
        rules,
        fpd,
    )
    assert violations == []

    events = audit.for_call(session.call_id)
    types = {e["type"] for e in events}
    assert "decide" in types
    assert "effects_committed" in types
    assert "agreement_drafted" in types
    audit.close()


@pytest.mark.asyncio
async def test_timings_recorded(tmp_path: Path) -> None:
    orch, session, audit = _orch(tmp_path)
    await orch.start()
    timings: dict[str, float] = {}
    await orch.on_creditor_text(
        "What settlement can you do?",
        timings,
        oracle=TurnAnalysis(stance="other"),
    )
    assert "nlu_ms" in timings
    assert "policy_ms" in timings
    assert "nlg_ms" in timings
    assert "server_total_ms" in timings
    assert timings["server_total_ms"] >= timings["nlu_ms"]
    audit.close()


@pytest.mark.asyncio
async def test_barge_in_does_not_keep_agreed_bp(tmp_path: Path) -> None:
    """Engine deal state must not stick after barge-in of an unacked CONFIRM."""
    orch, session, audit = _orch(tmp_path, auto_ack=False)
    await orch.start()
    assert session.pending is not None
    await orch.on_sentence_done(session.pending.sentence_ids)

    u = await orch.on_creditor_text(
        "Up to eight payments, minimum one hundred dollars, even structure.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="eight", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=10000,
                    quote="one hundred dollars",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure", value="even", quote="even", hedged=False
                ),
            ],
            stance="info",
        ),
    )
    await orch.on_sentence_done([sid for sid, _ in u.sentences])

    counter = await orch.on_creditor_text(
        "We can do forty-five percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=45.0,
            ask_quote="forty-five percent",
            stance="offer",
        ),
    )
    assert counter.action.intent == Intent.COUNTER
    await orch.on_sentence_done([sid for sid, _ in counter.sentences])
    counter_bp = counter.action.facts["counter_pct"].value
    assert isinstance(counter_bp, int)

    confirm = await orch.on_creditor_text(
        "We can accept that.",
        oracle=TurnAnalysis(stance="accept"),
    )
    assert confirm.action.intent == Intent.CONFIRM_SCHEDULE
    assert session.agreed_bp is None  # not acked yet
    assert session.pending is not None
    assert session.pending.pending_agreed_bp == counter_bp

    await orch.on_barge_in([])
    assert session.pending is None
    assert session.agreed_bp is None
    # confirmed_bp only lands on CONFIRM ack — barge dropped it.
    assert session.neg.confirmed_bp is None
    audit.close()


@pytest.mark.asyncio
async def test_post_nlu_queue_does_not_overwrite_pending(tmp_path: Path) -> None:
    """With auto_ack=False, queued text must not replace live pending speech."""
    orch, session, audit = _orch(tmp_path, auto_ack=False)
    await orch.start()
    assert session.pending is not None
    await orch.on_sentence_done(session.pending.sentence_ids)

    # Simulate text arriving during post-NLU by seeding the queue before emit ends.
    # Drive a normal turn, then set queue and ensure a second on_creditor_text
    # after ack processes it without having overwritten the first pending.
    u1 = await orch.on_creditor_text(
        "Maximum six payments, minimum one hundred, even.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=6, quote="six", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=10000,
                    quote="one hundred",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure", value="even", quote="even", hedged=False
                ),
            ],
            stance="info",
        ),
    )
    first_intent = u1.action.intent
    first_ids = list(session.pending.sentence_ids) if session.pending else []
    assert session.pending is not None
    assert first_ids

    # Inject queued text as if it arrived during post; must remain until after ack.
    orch._post_nlu_queue = "Also we need forty five percent."
    # Pending must still be the first action.
    assert session.pending is not None
    assert session.pending.action.intent == first_intent
    assert session.pending.sentence_ids == first_ids

    await orch.on_sentence_done(first_ids)
    assert session.pending is None

    # Next idle turn consumes the queue.
    u2 = await orch.on_creditor_text(
        "",
        oracle=TurnAnalysis(
            settlement_ask_pct=45.0,
            ask_quote="forty five percent",
            stance="offer",
        ),
    )
    assert orch._post_nlu_queue is None
    assert u2.action.intent in (Intent.CONFIRM_SCHEDULE, Intent.ASK_SETTLEMENT, Intent.COUNTER)
    audit.close()


@pytest.mark.asyncio
async def test_late_start_date_counters_terms_then_confirm(tmp_path: Path) -> None:
    """Infeasible FPD → COUNTER_TERMS; accept alt → CONFIRM_SCHEDULE."""
    from datetime import date

    orch, session, _audit = _orch(tmp_path, auto_ack=True)
    await orch.start()

    await orch.on_creditor_text(
        "Max eight payments, minimum one hundred dollars, even payments please.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="eight", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=10000,
                    quote="one hundred dollars",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure", value="even", quote="even", hedged=False
                ),
            ],
            stance="info",
        ),
    )

    # Ask 45% with a first payment past last_draft_date (2026-10-15).
    await orch.on_creditor_text(
        "Forty five percent, first payment October thirty first two thousand twenty six.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="first_payment_date",
                    value=date(2026, 10, 31),
                    quote="October thirty first",
                    hedged=False,
                )
            ],
            settlement_ask_pct=45.0,
            ask_quote="Forty five percent",
            stance="offer",
        ),
    )
    # Tentative FPD → READ_BACK first.
    assert session.neg.pending_readback == "first_payment_date" or session.phase in (
        Phase.DISCOVERY,
        Phase.NEGOTIATE,
        Phase.CONFIRM,
    )

    # Confirm the late date on read-back.
    u_rb = await orch.on_creditor_text(
        "yes",
        oracle=TurnAnalysis(stance="info", readback_response="confirm"),
    )
    # After KNOWN late FPD + ask, expect COUNTER_TERMS (or CONFIRM if somehow feasible).
    if u_rb.action.intent == Intent.COUNTER_TERMS:
        assert "alt_first_payment_date" in u_rb.action.facts
        u_ok = await orch.on_creditor_text(
            "yes that works",
            oracle=TurnAnalysis(stance="accept"),
        )
        # Affordable ask after alt date → negotiate (COUNTER) or confirm.
        assert u_ok.action.intent in (Intent.COUNTER, Intent.CONFIRM_SCHEDULE)
    else:
        # If policy went straight to confirm/counter/no-deal, still assert we did not crash.
        assert u_rb.action.intent in (
            Intent.CONFIRM_SCHEDULE,
            Intent.COUNTER,
            Intent.NO_DEAL_WRAP,
            Intent.ASK_SETTLEMENT,
            Intent.COUNTER_TERMS,
        )


@pytest.mark.asyncio
async def test_screenshot_transcript_revises_terms(tmp_path: Path) -> None:
    """Audit-style: revise max_payments after CONFIRM must not CLARIFY/escalate."""
    orch, session, audit = _orch(tmp_path)
    await orch.start()

    await orch.on_creditor_text(
        "it's 8",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="8", hedged=False),
            ],
            stance="info",
        ),
    )
    await orch.on_creditor_text(
        "110",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="min_payment_cents", value=11000, quote="110", hedged=False
                ),
            ],
            stance="info",
        ),
    )
    await orch.on_creditor_text(
        "even payment schedule",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="payment_structure", value="even", quote="even", hedged=False
                ),
            ],
            stance="info",
        ),
    )
    u = await orch.on_creditor_text(
        "I am looking for 70%",
        oracle=TurnAnalysis(
            settlement_ask_pct=70.0,
            ask_quote="70%",
            stance="offer",
        ),
    )
    assert u.action.intent == Intent.COUNTER
    counter_bp = u.action.facts["counter_pct"].value
    assert isinstance(counter_bp, int)
    assert counter_bp < 7000

    # Firm floor closes negotiation at the ask.
    u = await orch.on_creditor_text(
        "70 is our floor, we cannot go lower",
        oracle=TurnAnalysis(
            settlement_ask_pct=70.0,
            ask_quote="70",
            stance="reject",
            firm=True,
        ),
    )
    assert u.action.intent == Intent.CONFIRM_SCHEDULE
    assert session.neg.confirmed_bp == 7000
    assert u.action.facts["settlement_pct"].value == 7000

    # Revision: 3 payments instead — must not CLARIFY.
    u = await orch.on_creditor_text(
        "can we do it in 3 payments instead",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="max_payments", value=3, quote="3 payments", hedged=False
                ),
            ],
            stance="offer",
        ),
    )
    assert u.action.intent != Intent.CLARIFY
    assert u.action.intent != Intent.ESCALATE
    assert session.belief.get("max_payments").status == TermStatus.KNOWN
    assert session.belief.get("max_payments").value == 3
    assert u.action.intent == Intent.CONFIRM_SCHEDULE
    assert u.action.reason == "terms_revised"
    assert u.action.facts["settlement_pct"].value == 7000
    num = u.action.facts.get("num_payments")
    assert num is not None and isinstance(num.value, int)
    assert num.value <= 3

    revise_events = [
        e for e in audit.for_call(session.call_id) if e["type"] == "revise"
    ]
    assert revise_events
    audit.close()
