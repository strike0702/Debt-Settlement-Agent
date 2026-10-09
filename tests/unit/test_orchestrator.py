"""Orchestrator / session tests — oracle NLU + template NLG (no network)."""

from __future__ import annotations

from datetime import date
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
async def test_barge_in_keeps_eager_counter(tmp_path: Path) -> None:
    """offer_counter is eager; empty barge does not undo it; reject climbs ladder."""
    orch, session, audit = _orch(tmp_path, auto_ack=False)
    await orch.start()
    # Ack opening so phase advances.
    assert session.pending is not None
    await orch.on_sentence_done(session.pending.sentence_ids)

    # Soft rules → ask affordable; counter ladder (not term-alt recovery).
    utt = await orch.on_creditor_text(
        "Max six payments, minimum payment twenty five dollars, even payments.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=6, quote="six", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=2500,
                    quote="twenty five dollars",
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
        "We need seventy percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=70.0,
            ask_quote="seventy percent",
            stance="offer",
        ),
    )
    assert counter1.action.intent == Intent.COUNTER
    bp1 = counter1.action.facts["counter_pct"].value
    assert isinstance(bp1, int)
    assert session.neg.counters_offered == [bp1]

    # Barge-in: drop pending speech; offer_counter stays (idempotent).
    await orch.on_barge_in([])
    assert session.pending is None
    assert session.neg.counters_offered == [bp1]

    # The rep moves down, so the ladder concedes (a flat rep would get a hold).
    counter2 = await orch.on_creditor_text(
        "That is too low, we could do sixty-five percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=65.0,
            ask_quote="sixty-five percent",
            stance="reject",
        ),
    )
    assert counter2.action.intent == Intent.COUNTER
    bp2 = counter2.action.facts["counter_pct"].value
    assert isinstance(bp2, int)
    assert bp2 > bp1
    assert session.neg.counters_offered == [bp1, bp2]
    await orch.on_sentence_done([sid for sid, _ in counter2.sentences])
    # Ack must not double-append the same bp.
    assert session.neg.counters_offered == [bp1, bp2]
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
async def test_confirm_bookkeeping_eager_before_ack(tmp_path: Path) -> None:
    """CONFIRM record_confirm lands on emit so a fast accept can wrap."""
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
    assert session.pending is not None
    # Eager: confirmed before TTS ack so a typed "yes" wraps, not re-confirms.
    assert session.neg.confirmed_bp == counter_bp
    assert session.neg.phase == Phase.CONFIRM
    assert session.agreed_bp == counter_bp
    assert session.last_eval is not None

    wrap = await orch.on_creditor_text(
        "yes",
        oracle=TurnAnalysis(stance="accept"),
    )
    assert wrap.action.intent == Intent.PROPOSE_WRAP
    assert wrap.action.next_phase == Phase.WRAP
    await orch.on_sentence_done([sid for sid, _ in wrap.sentences])
    assert session.neg.phase == Phase.WRAP
    audit.close()


@pytest.mark.asyncio
async def test_barge_in_keeps_counter_if_heard(tmp_path: Path) -> None:
    """Partial barge after hearing a COUNTER must still record offer_counter."""
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
    counter_bp = counter.action.facts["counter_pct"].value
    assert isinstance(counter_bp, int)
    assert session.neg.counters_offered == [counter_bp]

    heard = [counter.sentences[0][0]]
    await orch.on_barge_in(heard)
    assert session.pending is None
    assert session.neg.counters_offered == [counter_bp]
    audit.close()


@pytest.mark.asyncio
async def test_post_nlu_queue_does_not_overwrite_pending(tmp_path: Path) -> None:
    """With auto_ack=False, queued text must not replace live pending speech."""
    orch, session, audit = _orch(tmp_path, auto_ack=False)
    await orch.start()
    assert session.pending is not None
    await orch.on_sentence_done(session.pending.sentence_ids)

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

    # Inject queued text as if it arrived during post; must remain until ack.
    orch._post_nlu_queue = "Also we need forty five percent."
    assert session.pending is not None
    assert session.pending.action.intent == first_intent
    assert session.pending.sentence_ids == first_ids

    await orch.on_sentence_done(first_ids)
    # F08: ack auto-drains the post-NLU queue (no extra idle message required).
    assert orch._post_nlu_queue is None
    audit.close()


@pytest.mark.asyncio
async def test_unacked_pending_barged_before_new_text(tmp_path: Path) -> None:
    """F07: second creditor line while pending unacked must barge, not overwrite."""
    orch, session, audit = _orch(tmp_path, auto_ack=False)
    await orch.start()
    assert session.pending is not None
    await orch.on_sentence_done(session.pending.sentence_ids)

    await orch.on_creditor_text(
        "Maximum six payments.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=6, quote="six", hedged=False),
            ],
            stance="info",
        ),
    )
    first_ids = list(session.pending.sentence_ids)
    assert first_ids

    u2 = await orch.on_creditor_text(
        "minimum one hundred dollars, even structure",
        oracle=TurnAnalysis(
            terms=[
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
    assert session.pending is not None
    assert session.pending.sentence_ids != first_ids
    assert u2.action.intent is not None
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
async def test_bare_min_payment_asks_dollars_or_cents(tmp_path: Path) -> None:
    """Bare '110' must clarify unit; 'dollars' then stores 11000 cents."""
    orch, session, audit = _orch(tmp_path)
    await orch.start()

    await orch.on_creditor_text(
        "4",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=4, quote="4", hedged=False),
            ],
            stance="info",
        ),
    )
    # Live NLU path is what flags ambiguity; simulate via VerifiedAnalysis by
    # using empty oracle terms + non-oracle would need LLM. Instead drive the
    # ambiguity action through a second turn that post_verify would produce:
    # use settings with a stub by calling the action path via empty terms and
    # injecting pending — easiest: use analyze fast-path via non-oracle is hard.
    # Call on_creditor_text with oracle that has bare quote "110" (triggers post_verify ambiguity).
    u = await orch.on_creditor_text(
        "110",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="min_payment_cents", value=110, quote="110", hedged=False
                ),
            ],
            stance="info",
        ),
    )
    assert u.action.intent == Intent.CLARIFY
    assert u.action.reason == "cents_ambiguity"
    assert session.neg.pending_cents_clarify is not None
    assert session.belief.get("min_payment_cents").status != TermStatus.KNOWN

    u2 = await orch.on_creditor_text(
        "dollars",
        oracle=TurnAnalysis(stance="info"),
    )
    assert session.belief.get("min_payment_cents").value == 11000
    assert session.neg.pending_cents_clarify is None
    assert u2.action.intent != Intent.CLARIFY or u2.action.reason != "cents_ambiguity"
    audit.close()


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
        "110 dollars",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="min_payment_cents",
                    value=11000,
                    quote="110 dollars",
                    hedged=False,
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

    # Firm floor at or below the line: one final counter, then the repeat is accepted.
    firm = TurnAnalysis(settlement_ask_pct=70.0, ask_quote="70", stance="reject", firm=True)
    u = await orch.on_creditor_text("70 is our floor, we cannot go lower", oracle=firm)
    assert u.action.intent == Intent.COUNTER
    assert u.action.reason == "final_counter"
    u = await orch.on_creditor_text("Still 70, that is our floor", oracle=firm)
    assert u.action.intent == Intent.CONFIRM_SCHEDULE
    assert session.neg.confirmed_bp == 7000
    assert u.action.facts["settlement_pct"].value == 7000

    # Revision: 3 payments instead — must not CLARIFY.
    # Revision to more payments keeps 70% within the accept line (Phase 45: a
    # revision that drops the line below 70% is a handoff, ``above_accept_line``).
    u = await orch.on_creditor_text(
        "can we do it in 10 payments instead",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="max_payments", value=10, quote="10 payments", hedged=False
                ),
            ],
            stance="offer",
        ),
    )
    assert u.action.intent != Intent.CLARIFY
    assert u.action.intent != Intent.ESCALATE
    assert session.belief.get("max_payments").status == TermStatus.KNOWN
    assert session.belief.get("max_payments").value == 10
    assert u.action.intent == Intent.CONFIRM_SCHEDULE
    assert u.action.reason == "terms_revised"
    assert u.action.facts["settlement_pct"].value == 7000
    num = u.action.facts.get("num_payments")
    assert num is not None and isinstance(num.value, int)
    assert num.value <= 10

    revise_events = [
        e for e in audit.for_call(session.call_id) if e["type"] == "revise"
    ]
    assert revise_events
    audit.close()


@pytest.mark.asyncio
async def test_balloon_high_min_offers_alt_fpd_not_rescue_escalate(
    tmp_path: Path,
) -> None:
    """balloon_structure + high min: default FPD empty curve but later FPD works.

    Must COUNTER_TERMS (not ESCALATE out_of_guardrail) so we can still negotiate.
    """
    from datetime import date

    scenario = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    audit = AuditLog(tmp_path / "audit.db")
    session = CallSession(scenario=scenario)
    orch = Orchestrator(
        session,
        llm=None,
        settings=_settings(),
        audit=audit,
        auto_ack=True,
    )
    await orch.start()

    await orch.on_creditor_text(
        "Five payments, minimum one hundred dollars, balloon is fine.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=5, quote="Five", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=10000,
                    quote="one hundred dollars",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure",
                    value="balloon",
                    quote="balloon",
                    hedged=False,
                ),
            ],
            stance="info",
        ),
    )

    u = await orch.on_creditor_text(
        "Eighty percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=80.0,
            ask_quote="Eighty percent",
            stance="offer",
        ),
    )
    assert u.action.intent == Intent.COUNTER_TERMS
    assert u.action.reason == "alt_first_payment_date"
    alt = u.action.facts["alt_first_payment_date"].value
    assert isinstance(alt, date)

    # Accept alt → should negotiate (counter), not escalate.
    u2 = await orch.on_creditor_text(
        "yes that start date works",
        oracle=TurnAnalysis(stance="accept"),
    )
    assert u2.action.intent in (Intent.COUNTER, Intent.CONFIRM_SCHEDULE)
    assert session.neg.phase != Phase.ESCALATE
    if u2.action.intent == Intent.COUNTER:
        bp = u2.action.facts["counter_pct"].value
        assert isinstance(bp, int)
        assert session.agreed_bp == bp
    audit.close()


@pytest.mark.asyncio
async def test_balloon_reject_fpd_cascades_to_min_or_max(tmp_path: Path) -> None:
    """Reject alt FPD → next term stage (min or max), not immediate escalate."""
    from datetime import date

    scenario = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    audit = AuditLog(tmp_path / "audit.db")
    session = CallSession(scenario=scenario)
    orch = Orchestrator(
        session,
        llm=None,
        settings=_settings(),
        audit=audit,
        auto_ack=True,
    )
    await orch.start()

    await orch.on_creditor_text(
        "Five payments, minimum one hundred dollars, balloon is fine.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=5, quote="Five", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=10000,
                    quote="one hundred dollars",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure",
                    value="balloon",
                    quote="balloon",
                    hedged=False,
                ),
            ],
            stance="info",
        ),
    )

    u = await orch.on_creditor_text(
        "Eighty percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=80.0,
            ask_quote="Eighty percent",
            stance="offer",
        ),
    )
    assert u.action.intent == Intent.COUNTER_TERMS
    assert u.action.reason == "alt_first_payment_date"

    u2 = await orch.on_creditor_text(
        "No, that start date will not work.",
        oracle=TurnAnalysis(stance="reject"),
    )
    # Cascade: min payment or max payments or escalate/no-deal — not stuck on FPD.
    assert u2.action.intent in (
        Intent.COUNTER_TERMS,
        Intent.ESCALATE,
        Intent.NO_DEAL_WRAP,
    )
    if u2.action.intent == Intent.COUNTER_TERMS:
        assert u2.action.reason in ("alt_min_payment_cents", "alt_max_payments")
        assert "alt_first_payment_date" not in u2.action.facts
    audit.close()


@pytest.mark.asyncio
async def test_balloon_weak_min_then_ask_50_offers_deeper_min(
    tmp_path: Path,
) -> None:
    """balloon_structure: jump past a token $50 floor to $30 so 50% is negotiable."""
    from datetime import date

    scenario = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    audit = AuditLog(tmp_path / "audit.db")
    session = CallSession(scenario=scenario)
    orch = Orchestrator(
        session,
        llm=None,
        settings=_settings(),
        audit=audit,
        auto_ack=True,
    )
    await orch.start()

    await orch.on_creditor_text(
        "Five payments, minimum one hundred dollars, balloon is fine.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=5, quote="Five", hedged=False),
                ExtractedTerm(
                    field="min_payment_cents",
                    value=10000,
                    quote="one hundred dollars",
                    hedged=False,
                ),
                ExtractedTerm(
                    field="payment_structure",
                    value="balloon",
                    quote="balloon",
                    hedged=False,
                ),
            ],
            stance="info",
        ),
    )

    u = await orch.on_creditor_text(
        "Eighty percent.",
        oracle=TurnAnalysis(
            settlement_ask_pct=80.0,
            ask_quote="Eighty percent",
            stance="offer",
        ),
    )
    # May be FPD or min depending on rebase; reject FPD to reach min alt.
    if u.action.intent == Intent.COUNTER_TERMS and u.action.reason == "alt_first_payment_date":
        u = await orch.on_creditor_text(
            "No, that start date will not work.",
            oracle=TurnAnalysis(stance="reject"),
        )
    assert u.action.intent == Intent.COUNTER_TERMS
    assert u.action.reason == "alt_min_payment_cents"
    # Jump to the floor that unlocks a real ceiling ($30), not a weak $50 step.
    first_min = u.action.facts["alt_min_payment_cents"].value
    assert isinstance(first_min, int)
    assert first_min == 3000

    u2 = await orch.on_creditor_text(
        "yes, thirty is fine. We need fifty percent minimum.",
        oracle=TurnAnalysis(
            stance="accept",
            settlement_ask_pct=50.0,
            ask_quote="fifty percent",
        ),
    )
    # After the min cut, 50% should be negotiable (counter or confirm), not 8% loop.
    assert u2.action.intent in (Intent.COUNTER, Intent.CONFIRM_SCHEDULE)
    if u2.action.intent == Intent.COUNTER:
        bp = u2.action.facts["counter_pct"].value
        assert isinstance(bp, int)
        assert bp > 800
    audit.close()


@pytest.mark.asyncio
async def test_balloon_fifty_dollar_min_accepts_later_start_above_eight_percent(
    tmp_path: Path,
) -> None:
    """$50 min + 80% ask: later start unlocks ~77%. Accepting it must not lock 8%."""
    from datetime import date

    scenario = load_scenario(
        "fixtures/scenarios/balloon_structure", rebase_to=date.today()
    )
    audit = AuditLog(tmp_path / "audit.db")
    session = CallSession(scenario=scenario)
    orch = Orchestrator(
        session,
        llm=None,
        settings=_settings(),
        audit=audit,
        auto_ack=True,
    )
    await orch.start()

    await orch.on_creditor_text(
        "Five payments.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(field="max_payments", value=5, quote="Five", hedged=False),
            ],
            stance="info",
        ),
    )
    await orch.on_creditor_text(
        "50 dollars.",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="min_payment_cents",
                    value=5000,
                    quote="50 dollars",
                    hedged=False,
                ),
            ],
            stance="info",
        ),
    )
    await orch.on_creditor_text(
        "balloon works",
        oracle=TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field="payment_structure",
                    value="balloon",
                    quote="balloon",
                    hedged=False,
                ),
            ],
            stance="info",
        ),
    )

    u = await orch.on_creditor_text(
        "80%",
        oracle=TurnAnalysis(
            settlement_ask_pct=80.0,
            ask_quote="80%",
            stance="offer",
        ),
    )
    assert u.action.intent == Intent.COUNTER_TERMS
    assert u.action.reason == "alt_first_payment_date"

    u2 = await orch.on_creditor_text(
        "okay",
        oracle=TurnAnalysis(stance="accept"),
    )
    assert u2.action.intent == Intent.COUNTER
    bp = u2.action.facts["counter_pct"].value
    assert isinstance(bp, int)
    assert bp > 800
    assert session.last_max_bp is not None and session.last_max_bp >= 6000

    u3 = await orch.on_creditor_text(
        "no we need 60%",
        oracle=TurnAnalysis(
            stance="reject",
            settlement_ask_pct=60.0,
            ask_quote="60%",
        ),
    )
    assert u3.action.intent in (Intent.COUNTER, Intent.CONFIRM_SCHEDULE)
    spoken_bp = (
        u3.action.facts["counter_pct"].value
        if u3.action.intent == Intent.COUNTER
        else u3.action.facts["settlement_pct"].value
    )
    assert isinstance(spoken_bp, int)
    assert spoken_bp > 800
    audit.close()


@pytest.mark.asyncio
async def test_denied_first_payment_readback_falls_back_to_engine_default(
    tmp_path: Path,
) -> None:
    """A denied date read-back leaves the field UNKNOWN; the engine must not crash.

    Seen in the Phase 24b A/B (``s0007_025``): ``build_rules`` succeeds without
    ``first_payment_date``, and the old ``assert isinstance(fpd, date)`` fired.
    """
    from feasibility.models import default_first_payment_date

    orch, session, audit = _orch(tmp_path)
    belief = session.belief
    belief.observe("max_payments", 6, "six", 1, verified=True, hedged=False)
    belief.observe("min_payment_cents", 2500, "$25", 1, verified=True, hedged=False)
    belief.observe("payment_structure", "even", "even", 1, verified=True, hedged=False)
    belief.observe(
        "first_payment_date", date(2026, 3, 31), "March 31", 1, verified=True, hedged=True
    )
    belief.confirm_readback("first_payment_date", yes=False)
    assert belief.get("first_payment_date").value is None

    summary = await orch._eval_bp(4500)

    assert summary is not None
    expected = default_first_payment_date(session.scenario.client)
    events = [
        e for e in audit.for_call(session.call_id)
        if e["type"] == "first_payment_date_default"
    ]
    assert events and events[0]["payload"]["value"] == expected.isoformat()


@pytest.mark.asyncio
async def test_nlu_llm_unavailable_rolls_back_turn(tmp_path: Path) -> None:
    """[P22] A turn whose NLU raises LLMUnavailable leaves no creditor line or turn bump."""
    from app.llm.client import FakeLLM, LLMUnavailable

    scenario = load_scenario("fixtures/demo")
    session = CallSession(scenario=scenario)
    llm = FakeLLM()  # empty nlu queue → LLMUnavailable
    orch = Orchestrator(
        session,
        llm=llm,
        settings=_settings(nlu_mode="llm"),
        audit=AuditLog(tmp_path / "audit.db"),
        auto_ack=True,
    )
    await orch.start()
    turn0, hist0 = session.neg.turn_idx, len(session.history)
    with pytest.raises(LLMUnavailable):
        await orch.on_creditor_text("We can take eight payments of some amount.")
    assert session.neg.turn_idx == turn0
    assert len(session.history) == hist0

    llm.enqueue(
        "nlu",
        '{"terms": [{"field": "max_payments", "value": 8, "quote": "eight",'
        ' "hedged": false}], "stance": "info"}',
    )
    await orch.on_creditor_text("Up to eight payments.")
    assert session.neg.turn_idx == turn0 + 1
    assert [t.text for t in session.history if t.role == "creditor"] == ["Up to eight payments."]


def _terms(**values: tuple[object, str]) -> list[ExtractedTerm]:
    """``field=(value, quote)``; the quote must appear in the utterance (verified)."""
    return [
        ExtractedTerm(field=f, value=v, quote=q, hedged=False)  # type: ignore[arg-type]
        for f, (v, q) in values.items()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "quote", "want"),
    [
        # The A/B wording: "the 100% balance" names the balance, not an ask, and the
        # NLU verifier drops it (Phase 26), so the agent still asks for the %.
        (
            "We require even payments to settle the 100% balance. Let me know if that works.",
            "100%",
            Intent.ASK_SETTLEMENT,
        ),
        # The policy path behind "Great, 100% is acceptable": an accepting line with
        # a real first number. Phase 45: countered, never accepted.
        ("We require even payments and can settle at 100%, that works.", "100%", Intent.COUNTER),
    ],
)
async def test_pair17_first_number_in_discovery_is_countered_not_accepted(
    tmp_path: Path, line: str, quote: str, want: Intent
) -> None:
    """A/B pair 17 (s0007_006) shape: the structure answer arrives with a 100% read
    as an accepting ask during discovery. Before Phase 45 the agent said "Great,
    100% is acceptable"; now the rep's first number always gets a counter."""
    orch, session, audit = _orch(tmp_path)
    await orch.start()
    await orch.on_creditor_text(
        "We can work with up to 7 payments of at least $94 each.",
        oracle=TurnAnalysis(
            terms=_terms(max_payments=(7, "7"), min_payment_cents=(9400, "$94")), stance="info"
        ),
    )
    u = await orch.on_creditor_text(
        line,
        oracle=TurnAnalysis(
            terms=_terms(payment_structure=("even", "even")),
            settlement_ask_pct=100.0,
            ask_quote=quote,
            stance="accept",
        ),
    )
    assert u.action.intent == want
    assert u.action.intent != Intent.CONFIRM_SCHEDULE
    assert session.agreement is None and session.neg.confirmed_bp is None
    if want == Intent.COUNTER:
        bp = u.action.facts["counter_pct"].value
        assert isinstance(bp, int) and bp < 10000
    audit.close()


@pytest.mark.asyncio
async def test_rep_turns_without_progress_hand_off(tmp_path: Path) -> None:
    """Loop guard (B): four rep turns in a row that add nothing → handoff ``no_progress``."""
    orch, session, audit = _orch(tmp_path)
    await orch.start()
    await orch.on_creditor_text(
        "Up to 6 payments, minimum $25, even payments.",
        oracle=TurnAnalysis(
            terms=_terms(
                max_payments=(6, "6"),
                min_payment_cents=(2500, "$25"),
                payment_structure=("even", "even"),
            ),
            stance="info",
        ),
    )
    ask = TurnAnalysis(settlement_ask_pct=70.0, ask_quote="70%", stance="offer")
    await orch.on_creditor_text("We are looking for 70%.", oracle=ask)
    intents = []
    for _ in range(4):
        u = await orch.on_creditor_text("Hmm, let me think about it.", oracle=TurnAnalysis())
        intents.append(u.action.intent)
    assert session.neg.no_progress_turns == 4
    assert intents[-1] == Intent.ESCALATE
    assert u.action.reason == "no_progress"
    assert Intent.ESCALATE not in intents[:-1]
    audit.close()


@pytest.mark.asyncio
async def test_rep_end_without_deal_is_a_handoff(tmp_path: Path) -> None:
    orch, session, audit = _orch(tmp_path)
    await orch.start()
    u = await orch.on_rep_end()
    assert u.action.intent == Intent.ESCALATE
    assert u.action.reason == "rep_ended"
    assert session.neg.phase == Phase.ESCALATE
    assert "specialist from our side will follow up" in " ".join(t for _, t in u.sentences)
    audit.close()


def test_offer_counter_ladder_bookkeeping_is_idempotent(tmp_path: Path) -> None:
    from app.agent.orchestrator import apply_effects
    from app.agent.policy import Effect

    _, session, audit = _orch(tmp_path)
    eff = Effect(
        kind="offer_counter",
        data={"bp": 3000, "ask_bp": 6000, "turn": 4, "stage": 1, "final_ask": 5500},
    )
    apply_effects(session, [eff])
    apply_effects(session, [eff])  # eager emit, then speech ack
    neg = session.neg
    assert neg.counters_offered == [3000] and neg.counter_turns == [4]
    assert (neg.ask_at_last_counter, neg.hold_stage, neg.final_counter_ask) == (6000, 1, 5500)
    apply_effects(session, [Effect(kind="note_question", data={"key": "ASK:max_payments"})])
    assert neg.question_counts == {"ASK:max_payments": 1}
    audit.close()
