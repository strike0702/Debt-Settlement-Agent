"""Turn pipeline: NLU → belief → affordability → policy → NLG → speech ack.

``Orchestrator`` owns the per-session ``asyncio.Lock``, cancel-and-merge when
new creditor text arrives during NLU, and defers most ``Action.effects`` until
``on_sentence_done``. COUNTER / CONFIRM / COUNTER_TERMS bookkeeping
(``offer_counter``, ``record_confirm``, ``set_phase``, …) applies eagerly on
emit so a fast typed or voice accept wraps instead of re-confirming mid-TTS.
Barge-in still keeps those kinds when any sentence was heard; other pending
effects are dropped. Each public turn method runs inside
``llm_call_scope(call_id)`` so every NLU / NLG call is audited under this call.
Every ``Utterance`` carries a ``TurnTrace`` (``app.schemas.events``) assembled
from what the turn already computed — NLU terms and drops, belief changes,
affordability, the decision with its ``reasons`` sentence, NLG template and
guard verdicts, timings — never by re-reading the audit log.
With ``Settings.nlg_h3`` (Phase 24b) the decided move gets optional ack /
answer acts (``app.agent.acts``) spoken before it, and LLM NLG sees the last
three public turns. Phase 46b: ``Settings.nlg_ack`` (default on) speaks the
code-built ack alone; the next rep turn may correct it ("No, it's six
payments" → the new value replaces the acked one; "that's not what I said" →
the acked terms go back to TENTATIVE and are read back). A reply to a pending
cents / amount question that also carries new terms is not just asked again:
the answer (if any) and the new terms are applied and ``decide`` runs.
Phase 45: before ``decide`` it counts rep turns in a row with no progress
(``policy.rep_turn_progress``) for the loop guard, runs ``policy.loop_guard``
on clarifies chosen before ``decide``, and turns a rep ending without a deal
(``on_rep_end``) or a wrap whose agreement fails to draft into a handoff
(``ESCALATE``). Does not own policy rules or LLM prompts — those live in
``policy`` / ``acts`` / ``nlu`` / ``nlg``.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import date
from typing import Any, Concatenate

from app.adapter.engine_adapter import (
    Affordability,
    EvalSummary,
    NeedsInfo,
    affordability,
    assumed_fields,
    build_rules,
    evaluate,
)
from app.adapter.validator import validate
from app.agent.acts import acked_fields, attach_acts, is_ack_correction, is_ack_dispute
from app.agent.guards import _cross_match
from app.agent.nlg import SAFE_FALLBACK, render_action, render_acts, speak_action
from app.agent.nlu import (
    VerifiedAnalysis,
    analyze,
    resolve_amounts,
    try_resolve_amount_clarify,
    try_resolve_cents_clarify,
)
from app.agent.nlu_types import TurnAnalysis
from app.agent.numbers import extract_tokens
from app.agent.policy import (
    AMOUNT_CLARIFY_KEY,
    Action,
    Agreement,
    Effect,
    Intent,
    Phase,
    amount_meaning_clarify_action,
    amount_meaning_escalate_action,
    ask_pct_to_bp,
    cents_ambiguity_clarify_action,
    decide,
    draft_agreement,
    field_already_countered,
    follow_up_action,
    loop_guard,
    opening_action,
    parse_pending_terms_value,
    rep_turn_progress,
    speak_schedule_action,
    terms_counter_key,
)
from app.agent.reasons import reason_key, reason_text
from app.agent.session import CallSession, PendingSpeech, Turn
from app.config import Settings, get_settings
from app.domain.belief import BeliefChange, TermStatus
from app.domain.facts import Fact
from app.domain.scenario import CallScenario
from app.llm.call_audit import llm_call_scope
from app.llm.client import LLMUnavailable, QueueWait, queue_wait_scope
from app.llm.prompts import NLG_CONTEXT_TURNS
from app.schemas.events import (
    Affordability as TraceAffordability,
)
from app.schemas.events import (
    CurvePoint,
    Decide,
    DroppedTerm,
    GuardResult,
    NlgTrace,
    SpokenSentence,
    TraceBeliefChange,
    TraceTerm,
    TurnTrace,
)
from app.store.audit import AuditLog
from feasibility.models import (
    CreditorRules,
    add_months,
    default_first_payment_date,
    end_of_month,
)


@dataclass
class Utterance:
    """Agent reply returned to CLI / WebSocket / tests."""

    sentences: list[tuple[str, str]]  # (sentence_id, text)
    action: Action
    timings: dict[str, float] = field(default_factory=dict)
    belief_changes: list[BeliefChange] = field(default_factory=list)
    agreement: Agreement | None = None
    # Decision trace for the WS ``turn_trace`` event (full; views filter it).
    trace: TurnTrace | None = None


# Affordability.curve is sampled at range(100, 10001, 100).
_CURVE_BPS = tuple(range(100, 10001, 100))


def _ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _close_timings(timings: dict[str, float], t_server: float, queue: QueueWait) -> None:
    """Fill ``server_total_ms`` and ``queue_ms``; default absent stages to 0.

    ``queue_ms`` is LLM limiter wait already inside ``nlu_ms`` / ``nlg_ms`` (not
    additive). ``engine_ms`` is affordability / term-alt search in
    ``_engine_context``; schedule eval for the chosen bp stays in ``policy_ms``.
    """
    for key in ("nlu_ms", "engine_ms", "policy_ms", "nlg_ms"):
        timings.setdefault(key, 0.0)
    timings["queue_ms"] = queue.ms
    timings["server_total_ms"] = _ms_since(t_server)


def find_alt_first_payment_date(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    requested: date,
    already: set[str],
) -> date | None:
    """Scan EOM candidates within the savings horizon.

    Prefers the closest date where ``ask_bp`` is feasible. When the ask
    fits nowhere, prefer the highest ceiling (then the closest date). The
    current month is usually closest and can be stuck at a token percent
    while a later month clears most of the ask. Never re-offers an ISO
    date in ``already``.
    """
    client = scenario.client
    start = end_of_month(max(client.as_of_date, client.first_draft_date))
    end = client.last_draft_date
    if start > end:
        return None

    candidates: list[date] = []
    cur = start
    # Cap scans (~24 months) so a bad fixture cannot hang the turn.
    for _ in range(24):
        if cur > end:
            break
        candidates.append(cur)
        nxt = end_of_month(add_months(cur, 1))
        if nxt <= cur:
            break
        cur = nxt

    best: date | None = None
    best_rank: tuple[int, int, int] | None = None
    for cand in candidates:
        iso = cand.isoformat()
        if iso in already:
            continue
        aff = affordability(scenario, rules, cand)
        if aff.max_bp is None:
            continue
        ask_ok = ask_bp is None or ask_bp in aff.feasible_bps
        dist = abs((cand - requested).days)
        # Ask-feasible: closest date. Otherwise a later month with a much
        # higher ceiling must beat the current month's token curve.
        if ask_ok:
            rank = (0, dist, -aff.max_bp)
        else:
            rank = (1, -aff.max_bp, dist)
        if best is None or best_rank is None or rank < best_rank:
            best = cand
            best_rank = rank
    return best


def find_alt_min_payment_cents(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    fpd: date,
    current: int,
    baseline_max_bp: int | None = None,
) -> int | None:
    """Search lower min-payment floors that unlock or improve affordability.

    Steps down by $10 (1000¢) toward the field floor (1000¢). Prefers the
    least invasive floor where ``ask_bp`` is feasible. When the ask fits
    nowhere, jump to the floor with the highest ceiling (then least
    invasive) — same idea as FPD — so we do not nickle-and-dime $50→$40→$30.

    When ``baseline_max_bp`` is set (curve already non-empty but ask still
    unreachable), only candidates that make the ask feasible or raise
    ``max_bp`` above the baseline count — avoids re-offering a useless cut.
    """
    floor = 1000
    if current <= floor:
        return None

    best: int | None = None
    # ask_ok → (0, distance, -max_bp); else → (1, -max_bp, distance)
    best_rank: tuple[int, int, int] | None = None
    cand = current - 1000
    scanned = 0
    while cand >= floor and scanned < 20:
        trial = dc_replace(rules, min_payment_cents=cand)
        aff = affordability(scenario, trial, fpd)
        if aff.max_bp is not None:
            ask_ok = ask_bp is None or ask_bp in aff.feasible_bps
            improved = baseline_max_bp is None or aff.max_bp > baseline_max_bp
            if ask_ok or improved:
                dist = current - cand
                if ask_ok:
                    rank = (0, dist, -aff.max_bp)
                else:
                    rank = (1, -aff.max_bp, dist)
                if best is None or best_rank is None or rank < best_rank:
                    best = cand
                    best_rank = rank
                    # Closest ask-ok wins; scanning high→low so first is enough.
                    if ask_ok:
                        return best
        cand -= 1000
        scanned += 1
    return best


def find_alt_max_payments(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    fpd: date,
    current: int,
    baseline_max_bp: int | None = None,
) -> int | None:
    """Search higher max-payment counts that unlock or improve affordability.

    Steps up by 1 toward 60. Prefers the lowest count where ``ask_bp`` is
    feasible. When the ask fits nowhere, jump to the lowest count that
    hits the best ceiling — skip useless +1 steps that still leave the
    ask unreachable (4→5 when only 6 unlocks a real ladder).

    When ``baseline_max_bp`` is set, only candidates that make the ask
    feasible or raise ``max_bp`` above the baseline count.
    """
    ceiling = 60
    if current >= ceiling:
        return None

    best: int | None = None
    # ask_ok → (0, distance, -max_bp); else → (1, -max_bp, distance)
    best_rank: tuple[int, int, int] | None = None
    cand = current + 1
    scanned = 0
    while cand <= ceiling and scanned < 20:
        # Keep "no token limit" semantics when token pays tracked max_payments.
        new_token = (
            cand if rules.max_token_pays == rules.max_payments else rules.max_token_pays
        )
        trial = dc_replace(
            rules,
            max_payments=cand,
            max_terms=cand,
            max_token_pays=new_token,
        )
        aff = affordability(scenario, trial, fpd)
        if aff.max_bp is not None:
            ask_ok = ask_bp is None or ask_bp in aff.feasible_bps
            improved = baseline_max_bp is None or aff.max_bp > baseline_max_bp
            if ask_ok or improved:
                dist = cand - current
                if ask_ok:
                    rank = (0, dist, -aff.max_bp)
                else:
                    rank = (1, -aff.max_bp, dist)
                if best is None or best_rank is None or rank < best_rank:
                    best = cand
                    best_rank = rank
                    # Lowest ask-ok wins; scanning low→high so first is enough.
                    if ask_ok:
                        return best
        cand += 1
        scanned += 1
    return best


def find_next_term_alt(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    fpd: date,
    max_payments: int,
    min_payment_cents: int,
    terms_countered: list[str],
    baseline_max_bp: int | None = None,
) -> tuple[str, Any] | None:
    """Next staggered non-price alt: FPD → lower min → higher max_payments.

    FPD is one try per field. Min / max may progress to a better value when
    a prior alt only unlocked a ceiling still below the ask.
    """
    if not field_already_countered(terms_countered, "first_payment_date"):
        already_iso = {
            k.split(":", 1)[1]
            for k in terms_countered
            if k.startswith("first_payment_date:")
        }
        alt = find_alt_first_payment_date(
            scenario,
            rules,
            ask_bp=ask_bp,
            requested=fpd,
            already=already_iso,
        )
        if alt is not None and alt != fpd:
            return ("first_payment_date", alt)

    alt_min = find_alt_min_payment_cents(
        scenario,
        rules,
        ask_bp=ask_bp,
        fpd=fpd,
        current=min_payment_cents,
        baseline_max_bp=baseline_max_bp,
    )
    if alt_min is not None and alt_min < min_payment_cents:
        key = terms_counter_key("min_payment_cents", alt_min)
        if key not in terms_countered:
            return ("min_payment_cents", alt_min)

    alt_max = find_alt_max_payments(
        scenario,
        rules,
        ask_bp=ask_bp,
        fpd=fpd,
        current=max_payments,
        baseline_max_bp=baseline_max_bp,
    )
    if alt_max is not None and alt_max > max_payments:
        key = terms_counter_key("max_payments", alt_max)
        if key not in terms_countered:
            return ("max_payments", alt_max)

    return None


_BOOKKEEPING_EFFECT_KINDS: frozenset[str] = frozenset(
    {
        "offer_counter",
        "record_confirm",
        "record_ask",
        "set_phase",
        "note_terms_countered",
    }
)


def apply_effects(session: CallSession, effects: list[Effect]) -> None:
    """Commit deferred negotiation side effects (speech ack path only)."""
    for effect in effects:
        kind = effect.kind
        data = effect.data
        if kind == "offer_counter":
            bp = int(data["bp"])
            # Idempotent: eager emit + barge-in/ack must not double-count.
            if not session.neg.counters_offered or session.neg.counters_offered[-1] != bp:
                session.neg.counters_offered.append(bp)
            # Phase 45 ladder bookkeeping: plain sets, so a second apply is a no-op.
            if "ask_bp" in data:
                session.neg.ask_at_last_counter = int(data["ask_bp"])
            if "stage" in data:
                session.neg.hold_stage = int(data["stage"])
            if "final_ask" in data:
                session.neg.final_counter_ask = int(data["final_ask"])
            turn = data.get("turn")
            if turn is not None and int(turn) not in session.neg.counter_turns:
                session.neg.counter_turns.append(int(turn))
        elif kind == "note_question":
            key = str(data["key"])
            session.neg.question_counts[key] = session.neg.question_counts.get(key, 0) + 1
        elif kind == "set_pending_readback":
            session.neg.pending_readback = str(data["field"])
        elif kind == "clear_pending_readback":
            session.neg.pending_readback = None
        elif kind == "inc_private_ask":
            session.neg.private_ask_count += 1
        elif kind == "inc_commit_demand":
            session.neg.commit_demand_count += 1
        elif kind == "record_ask":
            bp = int(data["bp"])
            session.neg.ask_bp = bp
            session.neg.ask_history.append(bp)
        elif kind == "record_confirm":
            raw_key = data.get("key")
            if isinstance(raw_key, list):
                session.neg.last_confirm_key = tuple(raw_key)
            elif isinstance(raw_key, tuple):
                session.neg.last_confirm_key = raw_key
            else:
                session.neg.last_confirm_key = (int(data["ask_bp"]),)
            session.neg.confirmed_bp = int(data["ask_bp"])
        elif kind == "note_confirm_accepted":
            session.neg.accepted_confirm_key = tuple(data["key"])
        elif kind == "inc_confirm_reject":
            session.neg.confirm_rejects += 1
        elif kind == "note_assumed_asked":
            session.neg.assumed_asked.add(str(data["field"]))
        elif kind == "note_clarify":
            fname = str(data["field"])
            session.neg.clarify_counts[fname] = session.neg.clarify_counts.get(fname, 0) + 1
        elif kind == "note_terms_countered":
            key = str(data.get("key") or terms_counter_key(str(data["field"]), data["value"]))
            if key not in session.neg.terms_countered:
                session.neg.terms_countered.append(key)
            session.neg.pending_terms_alt = {
                "field": str(data["field"]),
                "value": data["value"],
            }
        elif kind == "clear_pending_terms_alt":
            session.neg.pending_terms_alt = None
        elif kind == "clear_wrap":
            session.agreement = None
            session.agreed_bp = None
            session.last_eval = None
        elif kind == "set_pending_cents_clarify":
            session.neg.pending_cents_clarify = dict(data)
        elif kind == "clear_pending_cents_clarify":
            session.neg.pending_cents_clarify = None
        elif kind == "set_phase":
            session.neg.phase = Phase(str(data["phase"]))


def _carries_new_terms(verified: VerifiedAnalysis, *, held_cents: int | None = None) -> bool:
    """True when a reply brings a new ask or terms beyond the amount in question.

    ``held_cents``: the amount a pending "total or per payment?" is about; the
    NLU filing that same amount again is not news.
    """
    terms = [
        t
        for t in verified.terms
        if not (t.field == "min_payment_cents" and t.value == held_cents)
    ]
    return (
        bool(terms)
        or (verified.settlement_ask_pct is not None and verified.ask_verified)
        or verified.settlement_ask_total_cents not in (None, held_cents)
        or verified.amount_ambiguous_cents not in (None, held_cents)
    )


def _merge_cents_answer(
    verified: VerifiedAnalysis, resolved: VerifiedAnalysis
) -> VerifiedAnalysis:
    """This turn's analysis with the "dollars or cents?" answer swapped in.

    Only used when the reply also carries new terms: the rep's stance, ask and
    other terms stay; the answered field's term comes from ``resolved``.
    """
    field = resolved.terms[0].field
    terms = [*resolved.terms, *(t for t in verified.terms if t.field != field)]
    return verified.model_copy(
        update={"terms": terms, "cents_ambiguity_bare": None, "cents_ambiguity_field": None}
    )


def _restore_held_pct(verified: VerifiedAnalysis, pending: dict[str, Any]) -> VerifiedAnalysis:
    """Put back a % ask the dropped amount question held back (unless the rep's
    reply has its own ask, or the % was the disputed half of the question)."""
    if (
        pending.get("pct") is None
        or pending.get("trigger") == "pct_total_disagree"
        or (verified.settlement_ask_pct is not None and verified.ask_verified)
    ):
        return verified
    return verified.model_copy(
        update={
            "settlement_ask_pct": float(pending["pct"]),
            "ask_quote": pending.get("pct_quote"),
            "ask_verified": True,
        }
    )


def _merge_amount_answer(
    verified: VerifiedAnalysis, resolved: VerifiedAnalysis, *, held_cents: int | None = None
) -> VerifiedAnalysis:
    """This turn's analysis with the "total or per payment?" answer swapped in.

    Keeps the rep's stance, flags and other terms; the answer's amount (and
    any % ask it brings back) replaces the doubtful one. A new verified % ask in
    the same reply wins over both ("the total, but we could do 40%").
    """
    answered = {held_cents, *(t.value for t in resolved.terms)}
    if resolved.settlement_ask_total_cents is not None:
        answered.add(resolved.settlement_ask_total_cents)
    terms = [
        *resolved.terms,
        *(
            t
            for t in verified.terms
            if not (t.field == "min_payment_cents" and (held_cents is None or t.value in answered))
        ),
    ]
    own_ask = verified.settlement_ask_pct is not None and verified.ask_verified
    update: dict[str, Any] = {
        "terms": terms,
        "settlement_ask_total_cents": None if own_ask else resolved.settlement_ask_total_cents,
        "ask_total_quote": None if own_ask else resolved.ask_total_quote,
        "amount_ambiguous_cents": None,
        "amount_ambiguous_quote": None,
    }
    if own_ask:
        return verified.model_copy(update=update)
    if resolved.settlement_ask_pct is not None:
        update.update(
            settlement_ask_pct=resolved.settlement_ask_pct,
            ask_quote=resolved.ask_quote,
            ask_verified=True,
        )
    elif resolved.settlement_ask_total_cents is not None:
        update.update(settlement_ask_pct=None, ask_quote=None, ask_verified=False)
    return verified.model_copy(update=update)


def _call_scoped[**P, R](
    fn: Callable[Concatenate[Orchestrator, P], Awaitable[R]],
) -> Callable[Concatenate[Orchestrator, P], Awaitable[R]]:
    """Run a public turn method inside ``llm_call_scope(session.call_id)``.

    Every LLM call it makes (NLU, NLG) is then audited under this call id.
    """

    @functools.wraps(fn)
    async def wrapper(self: Orchestrator, *args: P.args, **kwargs: P.kwargs) -> R:
        with llm_call_scope(self.session.call_id):
            return await fn(self, *args, **kwargs)

    return wrapper


class Orchestrator:
    """Drive one ``CallSession`` through start / text / ack / barge-in."""

    def __init__(
        self,
        session: CallSession,
        *,
        llm: Any | None = None,
        settings: Settings | None = None,
        audit: AuditLog | None = None,
        auto_ack: bool = False,
    ) -> None:
        self.session = session
        self.llm = llm
        self.settings = settings or get_settings()
        self.audit = audit
        self.auto_ack = auto_ack
        # ``_lock`` serializes the turn pipeline; ``_meta`` protects stage / merge
        # so concurrent text can bump ``_nlu_gen`` while NLU runs with lock released.
        self._lock = asyncio.Lock()
        self._meta = asyncio.Lock()
        self._stage: str = "idle"  # idle | nlu | post
        self._nlu_text: str = ""
        self._nlu_gen: int = 0
        self._post_nlu_queue: str | None = None
        self._result_fut: asyncio.Future[Utterance] | None = None
        self._ref: date = session.scenario.client.as_of_date
        # Engine eval for the in-flight turn; committed to session only on speech ack.
        self._turn_eval: EvalSummary | None = None
        self._turn_agreed_bp: int | None = None
        # Limiter-wait accumulator for the turn in flight (see ``_close_timings``).
        self._turn_queue = QueueWait()
        # Turns run by the post-NLU drain in ``on_sentence_done``; the WS emits them.
        self._drained: list[Utterance] = []
        # What the turn in flight computed, for ``Utterance.trace`` (see ``_build_trace``).
        self._trace_ctx: dict[str, Any] = {}
        # Belief field → value the last emitted move acknowledged (Phase 46b); the
        # next rep turn may correct it. Cleared when that turn starts.
        self._last_ack: dict[str, Any] = {}

    def _begin_trace(self, creditor_text: str | None) -> None:
        self._trace_ctx = {"creditor_text": creditor_text}

    def _audit(self, actor: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        if self.audit is not None:
            self.audit.append(self.session.call_id, actor, event_type, payload)

    @_call_scoped
    async def start(self) -> Utterance:
        """Produce the OPENING line; effects apply on ack (or auto-ack)."""
        async with self._lock:
            t0 = time.perf_counter()
            self._begin_trace(None)
            action = opening_action(settings=self.settings)
            action = action.model_copy(
                update={
                    "effects": list(action.effects)
                    + [Effect(kind="set_phase", data={"phase": Phase.DISCOVERY.value})],
                }
            )
            with queue_wait_scope() as queue:
                sentences = await self._speak(action, last_rep_line="")
            timings = {"nlg_ms": _ms_since(t0)}
            _close_timings(timings, t0, queue)
            self._audit(
                "orchestrator",
                "start",
                {"intent": action.intent.value, "timings": timings},
            )
            return await self._emit(action, sentences, timings, belief_changes=[])

    @_call_scoped
    async def on_creditor_text(
        self,
        text: str,
        timings: dict[str, float] | None = None,
        *,
        oracle: TurnAnalysis | None = None,
    ) -> Utterance:
        """NLU → belief → afford → decide → NLG.

        Text that arrives while NLU runs is merged and NLU reruns on the concat;
        every merged caller gets the same ``Utterance`` (callers emit it once).
        Text that arrives after NLU (post stage) is queued, the caller gets the
        in-flight turn's ``Utterance``, and the queue drains after speech ack
        (``on_sentence_done`` → ``pop_drained``). The voice WS runs handlers as
        concurrent tasks, so both paths are live.
        """
        out_timings = timings if timings is not None else {}

        # F07: new creditor text while prior TTS unacked — barge first so pending
        # is not overwritten without applying/dropping effects.
        if self.session.pending is not None:
            await self.on_barge_in(list(self.session.pending.acked))

        async with self._meta:
            merge_fut: asyncio.Future[Utterance] | None = None
            if self._stage == "nlu":
                self._nlu_text = f"{self._nlu_text} {text}".strip()
                self._nlu_gen += 1
                self._audit(
                    "orchestrator",
                    "nlu_cancel_merge",
                    {"merged": self._nlu_text},
                )
                merge_fut = self._result_fut
            elif self._stage == "post":
                self._post_nlu_queue = (
                    f"{self._post_nlu_queue} {text}".strip()
                    if self._post_nlu_queue
                    else text
                )
                self._audit(
                    "orchestrator",
                    "text_queued",
                    {"queued": self._post_nlu_queue},
                )
                merge_fut = self._result_fut
        if merge_fut is not None:
            return await merge_fut

        await self._lock.acquire()
        loop = asyncio.get_running_loop()
        async with self._meta:
            self._result_fut = loop.create_future()
            if self._post_nlu_queue:
                text = (
                    f"{self._post_nlu_queue} {text}".strip()
                    if text
                    else self._post_nlu_queue
                )
                self._post_nlu_queue = None
        try:
            with queue_wait_scope() as queue:
                self._turn_queue = queue
                result = await self._run_turn(text, out_timings, oracle=oracle)
            async with self._meta:
                if self._result_fut is not None and not self._result_fut.done():
                    self._result_fut.set_result(result)
            return result
        except Exception as e:
            async with self._meta:
                if self._result_fut is not None and not self._result_fut.done():
                    self._result_fut.set_exception(e)
            raise
        finally:
            async with self._meta:
                self._stage = "idle"
                self._result_fut = None
            self._lock.release()

    async def _run_turn(
        self,
        text: str,
        timings: dict[str, float],
        *,
        oracle: TurnAnalysis | None,
    ) -> Utterance:
        """Caller must hold ``self._lock``. Releases it only during NLU await."""
        session = self.session
        t_server = time.perf_counter()
        acked, self._last_ack = self._last_ack, {}

        while True:
            session.neg.turn_idx += 1
            turn = session.neg.turn_idx
            working = text

            session.history.append(Turn(role="creditor", text=working, spoken=True))
            self._begin_trace(working)
            self._ingest_creditor_numbers(working)
            self._audit("creditor", "utterance", {"text": working, "turn": turn})

            # --- NLU (pipeline lock released so concurrent text can merge) ---
            async with self._meta:
                self._stage = "nlu"
                self._nlu_text = working
                gen = self._nlu_gen
            t_nlu = time.perf_counter()

            self._lock.release()
            try:
                try:
                    verified = await analyze(
                        working,
                        session.last_agent_line(),
                        session.neg.pending_readback,
                        llm=self.llm,
                        settings=self.settings,
                        oracle=oracle,
                        audit=self.audit,
                        call_id=session.call_id,
                        ref=self._ref,
                    )
                finally:
                    await self._lock.acquire()
            except LLMUnavailable:
                # The turn never happened: drop its creditor line and turn bump so
                # turn numbers do not skip and a retry starts clean.
                if session.history and session.history[-1].role == "creditor":
                    session.history.pop()
                session.neg.turn_idx -= 1
                raise

            timings["nlu_ms"] = _ms_since(t_nlu)

            # Cancel-and-merge: new text arrived during NLU — rerun on concat.
            async with self._meta:
                merged = self._nlu_text
                restarted = self._nlu_gen != gen
            if restarted:
                self._audit(
                    "orchestrator",
                    "nlu_restart",
                    {"merged": merged, "stale_turn": turn},
                )
                # Roll back the stale creditor history line and turn bump.
                if session.history and session.history[-1].role == "creditor":
                    session.history.pop()
                session.neg.turn_idx -= 1
                text = merged
                oracle = None  # merged free-text; oracle was for the first fragment
                continue

            async with self._meta:
                self._stage = "post"
            self._trace_ctx["verified"] = verified
            self._audit(
                "nlu",
                "analysis",
                {
                    "turn": turn,
                    "stance": verified.stance,
                    "terms": [t.model_dump(mode="json") for t in verified.terms],
                    "settlement_ask_pct": verified.settlement_ask_pct,
                    "cents_ambiguity_bare": verified.cents_ambiguity_bare,
                    "nlu_ms": timings["nlu_ms"],
                },
            )

            # Resolve / emit dollars-vs-cents clarify for bare money replies.
            if session.neg.pending_cents_clarify is not None:
                resolved = try_resolve_cents_clarify(
                    working, session.neg.pending_cents_clarify, ref=self._ref
                )
                moved_on = _carries_new_terms(verified)
                if resolved is not None and resolved.terms:
                    # Answer plus new terms: keep the rep's turn, swap the answer in.
                    verified = (
                        _merge_cents_answer(verified, resolved) if moved_on else resolved
                    )
                    self._trace_ctx["verified"] = verified
                    session.neg.pending_cents_clarify = None
                    self._audit(
                        "nlu",
                        "cents_clarify_resolved",
                        {
                            "field": resolved.terms[0].field,
                            "value": resolved.terms[0].value,
                        },
                    )
                elif resolved is not None and not resolved.terms:
                    # Chose an out-of-range reading — drop pending and re-ask.
                    session.neg.pending_cents_clarify = None
                    self._audit("nlu", "cents_clarify_out_of_range", {})
                elif moved_on:
                    # No answer, but a new ask or terms: take the turn, drop the question.
                    field = str(session.neg.pending_cents_clarify["field"])
                    session.neg.pending_cents_clarify = None
                    self._audit(
                        "nlu", "cents_clarify_dropped", {"field": field, "reason": "new_terms"}
                    )
                else:
                    pending = session.neg.pending_cents_clarify
                    action = loop_guard(
                        cents_ambiguity_clarify_action(
                            field=str(pending["field"]),
                            bare=int(pending["bare"]),
                        ),
                        session.neg,
                        self.settings,
                    )
                    t_nlg = time.perf_counter()
                    sentences = await self._speak(action, last_rep_line=working)
                    timings["policy_ms"] = 0.0
                    timings["nlg_ms"] = _ms_since(t_nlg)
                    _close_timings(timings, t_server, self._turn_queue)
                    return await self._emit(action, sentences, timings, [])

            if (
                verified.cents_ambiguity_bare is not None
                and verified.cents_ambiguity_field is not None
            ):
                action = loop_guard(
                    cents_ambiguity_clarify_action(
                        field=verified.cents_ambiguity_field,
                        bare=verified.cents_ambiguity_bare,
                    ),
                    session.neg,
                    self.settings,
                )
                # Eager: so a fast reply still resolves even if TTS ack is late.
                for eff in action.effects:
                    if eff.kind == "set_pending_cents_clarify":
                        apply_effects(session, [eff])
                t_nlg = time.perf_counter()
                sentences = await self._speak(action, last_rep_line=working)
                timings["policy_ms"] = 0.0
                timings["nlg_ms"] = _ms_since(t_nlg)
                _close_timings(timings, t_server, self._turn_queue)
                self._audit(
                    "policy",
                    "decide",
                    {
                        "intent": action.intent.value,
                        "reason": action.reason,
                        "next_phase": action.next_phase.value,
                        "policy_ms": 0.0,
                    },
                )
                return await self._emit(action, sentences, timings, [])

            # Total-vs-per-payment amounts (Phase 39): answer a pending question,
            # then turn a dollar-total ask into bp or ask about a doubtful amount.
            check_min_payment = True
            pending_amount = session.neg.pending_amount_clarify
            if pending_amount is not None:
                resolved_amount = try_resolve_amount_clarify(
                    working, pending_amount, ref=self._ref
                )
                # A closing, private ask, lock-in demand or hostility outranks the
                # question: drop it and let ``decide`` handle the turn.
                interrupts = (
                    verified.wants_to_end
                    or verified.asks_client_private_info
                    or verified.demands_commitment
                    or verified.hostility >= self.settings.hostility_threshold
                )
                held_cents = int(pending_amount["cents"])
                if resolved_amount is None and interrupts:
                    session.neg.pending_amount_clarify = None
                    self._audit(
                        "nlu",
                        "amount_clarify_dropped",
                        {"cents": held_cents, "reason": "interrupt"},
                    )
                elif resolved_amount is None and _carries_new_terms(
                    verified, held_cents=held_cents
                ):
                    # No answer, but a new ask or terms: take the turn (resolve_amounts
                    # still checks them) instead of repeating the question.
                    session.neg.pending_amount_clarify = None
                    verified = _restore_held_pct(verified, pending_amount)
                    self._audit(
                        "nlu",
                        "amount_clarify_dropped",
                        {"cents": held_cents, "reason": "new_terms"},
                    )
                elif resolved_amount is None:
                    if session.neg.clarify_counts.get(AMOUNT_CLARIFY_KEY, 0) >= 2:
                        session.neg.pending_amount_clarify = None
                        action = amount_meaning_escalate_action()
                    else:
                        action = amount_meaning_clarify_action(
                            cents=int(pending_amount["cents"])
                        )
                    return await self._emit_preempted(action, working, timings, t_server, [])
                else:
                    session.neg.pending_amount_clarify = None
                    check_min_payment = False
                    verified = _merge_amount_answer(
                        verified, resolved_amount, held_cents=held_cents
                    )
                    self._audit(
                        "nlu",
                        "amount_clarify_resolved",
                        {
                            "total_cents": resolved_amount.settlement_ask_total_cents,
                            "min_payment_cents": next(
                                (t.value for t in resolved_amount.terms), None
                            ),
                        },
                    )
            verified, amount_pending = resolve_amounts(
                verified,
                working,
                balance_cents=session.scenario.creditor_balance_cents,
                check_min_payment=check_min_payment,
                audit=self.audit,
                call_id=session.call_id,
            )
            self._trace_ctx["verified"] = verified
            if amount_pending is not None:
                # Eager, like the cents clarify: a fast reply must still resolve.
                session.neg.pending_amount_clarify = amount_pending
                belief_changes = self._apply_belief(
                    verified, turn, utterance=working, acked=acked
                )
                session.last_belief_changes = belief_changes
                action = amount_meaning_clarify_action(cents=int(amount_pending["cents"]))
                return await self._emit_preempted(
                    action, working, timings, t_server, belief_changes
                )

            belief_changes = self._apply_belief(verified, turn, utterance=working, acked=acked)
            session.last_belief_changes = belief_changes

            # Accept a pending non-price alternative before affordability.
            # That "yes" is about the term, not the last price counter.
            accepted_term_alt = False
            if (
                session.neg.pending_terms_alt is not None
                and verified.stance == "accept"
            ):
                pending = session.neg.pending_terms_alt
                alt_field = str(pending["field"])
                alt_value = parse_pending_terms_value(pending)
                ch = session.belief.accept_alternative(
                    alt_field,
                    alt_value,
                    f"accepted alternate {alt_field}",
                    turn,
                )
                belief_changes.append(ch)
                self._audit("belief", "accept_terms_alt", ch.model_dump(mode="json"))
                session.neg.pending_terms_alt = None
                accepted_term_alt = True

            self._turn_eval = None
            self._turn_agreed_bp = None
            t_eng = time.perf_counter()
            afford, rescue_ok, term_alt = await self._engine_context(verified)
            timings["engine_ms"] = _ms_since(t_eng)
            self._trace_ctx["afford"] = afford

            t_pol = time.perf_counter()
            analysis = verified.to_turn_analysis()
            # Loop guard (Phase 45): rep turns in a row that added nothing.
            if rep_turn_progress(
                analysis, session.neg, belief_changes, accepted_term_alt=accepted_term_alt
            ):
                session.neg.no_progress_turns = 0
            else:
                session.neg.no_progress_turns += 1
            action = decide(
                session.belief,
                session.neg,
                analysis,
                afford,
                settings=self.settings,
                rescue_within_guardrail=rescue_ok,
                term_alt=term_alt,
                accepted_term_alt=accepted_term_alt,
            )
            action = await self._enrich_action(action)
            if self.settings.nlg_h3 or self.settings.nlg_ack:
                # Presentation only: the move, its facts and effects are unchanged.
                # ``nlg_ack`` alone: the code-built ack, no answer act.
                action = attach_acts(
                    action,
                    verified,
                    belief_changes,
                    creditor_numbers=session.creditor_numbers,
                    private_blocklist=session.private_blocklist,
                    with_answer=self.settings.nlg_h3,
                )
                self._last_ack = acked_fields(action.ack)
            timings["policy_ms"] = _ms_since(t_pol)
            decide_payload: dict[str, Any] = {
                "intent": action.intent.value,
                "reason": action.reason,
                "next_phase": action.next_phase.value,
                "policy_ms": timings["policy_ms"],
            }
            if action.ack or action.answer is not None:
                decide_payload["acts"] = {
                    "ack": sorted(action.ack),
                    "answer": action.answer.topic if action.answer else None,
                }
            self._audit("policy", "decide", decide_payload)

            t_nlg = time.perf_counter()
            sentences = await self._speak(action, last_rep_line=working)
            timings["nlg_ms"] = _ms_since(t_nlg)
            _close_timings(timings, t_server, self._turn_queue)
            self._audit(
                "orchestrator",
                "turn_complete",
                {
                    "intent": action.intent.value,
                    "sentences": list(sentences),
                    "timings": dict(timings),
                },
            )

            utterance = await self._emit(action, sentences, timings, belief_changes)

            # Only chain queued text when the prior utterance is already acked
            # (auto_ack). Otherwise leave the queue for the next idle turn so
            # pending effects are not overwritten.
            if self.auto_ack:
                async with self._meta:
                    queued = self._post_nlu_queue
                    self._post_nlu_queue = None
                if queued:
                    follow = await self._run_turn(queued, {}, oracle=None)
                    return follow

            return utterance

    async def _emit_preempted(
        self,
        action: Action,
        working: str,
        timings: dict[str, float],
        t_server: float,
        belief_changes: list[BeliefChange],
    ) -> Utterance:
        """Speak and emit a move chosen before ``decide`` (a clarify or its escalation)."""
        action = loop_guard(action, self.session.neg, self.settings)
        t_nlg = time.perf_counter()
        sentences = await self._speak(action, last_rep_line=working)
        timings["policy_ms"] = 0.0
        timings["nlg_ms"] = _ms_since(t_nlg)
        _close_timings(timings, t_server, self._turn_queue)
        self._audit(
            "policy",
            "decide",
            {
                "intent": action.intent.value,
                "reason": action.reason,
                "next_phase": action.next_phase.value,
                "policy_ms": 0.0,
            },
        )
        return await self._emit(action, sentences, timings, belief_changes)

    def _commit_pending(self, pending: PendingSpeech) -> Agreement | None:
        """Apply effects + deferred eval; draft agreement or fail WRAP to a handoff."""
        apply_effects(self.session, pending.action.effects)
        if pending.pending_eval is not None:
            self.session.last_eval = pending.pending_eval
        if pending.pending_agreed_bp is not None:
            self.session.agreed_bp = pending.pending_agreed_bp

        agreement = self._maybe_draft_agreement(pending.action)
        if (
            pending.action.intent == Intent.PROPOSE_WRAP
            or (
                pending.action.intent == Intent.CLOSE
                and pending.action.reason == "thanks_accept"
            )
        ) and agreement is None:
            # No valid agreement behind the wrap: hand off (deal-or-handoff, Phase 45).
            self.session.neg.phase = Phase.ESCALATE
            self._audit(
                "orchestrator",
                "wrap_failed_no_deal",
                {"reason": "draft_failed", "phase": Phase.ESCALATE.value},
            )
        else:
            self.session.neg.phase = pending.action.next_phase

        self._audit(
            "orchestrator",
            "effects_committed",
            {
                "intent": pending.action.intent.value,
                "phase": self.session.neg.phase.value,
                "effects": [e.model_dump() for e in pending.action.effects],
            },
        )
        return agreement

    @_call_scoped
    async def on_sentence_done(self, ids: list[str] | set[str]) -> Agreement | None:
        """Ack spoken sentence ids; commit effects when all pending are done."""
        queued: str | None = None
        async with self._lock:
            pending = self.session.pending
            if pending is None:
                return None
            for sid in ids:
                pending.acked.add(sid)
                for turn in self.session.history:
                    if turn.sentence_id == sid:
                        turn.spoken = True
            self._audit(
                "orchestrator",
                "sentence_done",
                {"ids": list(ids), "acked": sorted(pending.acked)},
            )
            if not set(pending.sentence_ids).issubset(pending.acked):
                return None

            agreement = self._commit_pending(pending)
            self.session.pending = None
            async with self._meta:
                queued = self._post_nlu_queue
                self._post_nlu_queue = None
        # F08: drain text that arrived during post-NLU now that speech is acked.
        if queued:
            self._audit("orchestrator", "post_nlu_drain", {"queued": queued[:120]})
            drain_oracle = (
                TurnAnalysis(stance="other")
                if self.settings.nlu_mode == "oracle"
                else None
            )
            drained = await self.on_creditor_text(queued, timings=None, oracle=drain_oracle)
            self._drained.append(drained)
        return agreement

    def pop_drained(self) -> list[Utterance]:
        """Utterances produced by post-NLU drains since the last call (oldest first)."""
        out, self._drained = self._drained, []
        return out

    @_call_scoped
    async def on_barge_in(self, spoken_ids: list[str] | set[str]) -> None:
        """Keep spoken ids; drop unspoken sentences.

        If the rep heard any sentence of a COUNTER / CONFIRM, still commit the
        offer/confirm bookkeeping effects so a follow-up accept does not latch
        onto a stale prior counter.
        """
        async with self._lock:
            pending = self.session.pending
            spoken = set(spoken_ids)
            if pending is None:
                self._audit("orchestrator", "barge_in", {"spoken_ids": list(spoken)})
                return

            for turn in self.session.history:
                if turn.sentence_id is not None and turn.sentence_id in pending.sentence_ids:
                    turn.spoken = turn.sentence_id in spoken

            heard = bool(spoken.intersection(pending.sentence_ids))
            kept: list[Effect] = []
            if heard and pending.action.intent in (
                Intent.COUNTER,
                Intent.CONFIRM_SCHEDULE,
                Intent.COUNTER_TERMS,
            ):
                kept = [
                    e for e in pending.action.effects if e.kind in _BOOKKEEPING_EFFECT_KINDS
                ]
                if kept:
                    apply_effects(self.session, kept)
                    if (
                        pending.action.intent == Intent.CONFIRM_SCHEDULE
                        and pending.pending_eval is not None
                    ):
                        self.session.last_eval = pending.pending_eval
                    if (
                        pending.action.intent == Intent.CONFIRM_SCHEDULE
                        and pending.pending_agreed_bp is not None
                    ):
                        self.session.agreed_bp = pending.pending_agreed_bp

            dropped = pending.action
            self.session.pending = None
            kept_ids = {id(e) for e in kept}
            self._audit(
                "orchestrator",
                "barge_in",
                {
                    "spoken_ids": list(spoken),
                    "dropped_intent": dropped.intent.value,
                    "kept_effects": [e.model_dump() for e in kept],
                    "dropped_effects": [
                        e.model_dump()
                        for e in dropped.effects
                        if id(e) not in kept_ids
                    ],
                },
            )

    @_call_scoped
    async def on_rep_end(self) -> Utterance:
        """Rep ended the chat: close after a proposal, else hand off (a specialist follows up)."""
        async with self._lock:
            t0 = time.perf_counter()
            self._begin_trace(None)
            if self.session.neg.phase == Phase.WRAP:
                action = Action(
                    intent=Intent.CLOSE,
                    effects=[Effect(kind="set_phase", data={"phase": Phase.END.value})],
                    next_phase=Phase.END,
                    reason="rep_ended_after_wrap",
                )
            else:
                # No deal on the table: a handoff, never a bare no-deal end (Phase 45).
                action = follow_up_action(reason="rep_ended")
            with queue_wait_scope() as queue:
                sentences = await self._speak(action, last_rep_line="")
            timings = {"nlg_ms": _ms_since(t0)}
            _close_timings(timings, t0, queue)
            self._audit(
                "orchestrator",
                "call_ended",
                {"by": "rep", "intent": action.intent.value},
            )
            return await self._emit(action, sentences, timings, belief_changes=[])

    async def _emit(
        self,
        action: Action,
        sentences: list[str],
        timings: dict[str, float],
        belief_changes: list[BeliefChange],
    ) -> Utterance:
        pairs: list[tuple[str, str]] = []
        for text in sentences:
            sid = str(uuid.uuid4())
            pairs.append((sid, text))
            self.session.history.append(
                Turn(role="agent", text=text, spoken=False, sentence_id=sid)
            )

        self.session.pending = PendingSpeech(
            action=action,
            sentence_ids=[sid for sid, _ in pairs],
            pending_eval=self._turn_eval,
            pending_agreed_bp=self._turn_agreed_bp,
        )
        # Eager READ_BACK pending so barge-in doesn't lose the field before ack.
        if action.intent == Intent.READ_BACK:
            field = action.reason
            if field:
                self.session.neg.pending_readback = field
        # Eager COUNTER / CONFIRM / COUNTER_TERMS bookkeeping so a fast typed/voice
        # accept wraps (or accepts a term alt) while TTS is still playing.
        if action.intent in (
            Intent.COUNTER,
            Intent.CONFIRM_SCHEDULE,
            Intent.COUNTER_TERMS,
        ):
            kept = [
                e for e in action.effects if e.kind in _BOOKKEEPING_EFFECT_KINDS
            ]
            if kept:
                apply_effects(self.session, kept)
            if (
                action.intent == Intent.CONFIRM_SCHEDULE
                and self._turn_eval is not None
            ):
                self.session.last_eval = self._turn_eval
            if (
                action.intent == Intent.CONFIRM_SCHEDULE
                and self._turn_agreed_bp is not None
            ):
                self.session.agreed_bp = self._turn_agreed_bp
        result = Utterance(
            sentences=pairs,
            action=action,
            timings=dict(timings),
            belief_changes=belief_changes,
            agreement=self.session.agreement,
            trace=self._build_trace(action, pairs, timings, belief_changes),
        )
        if self.auto_ack:
            # Nested ack while holding turn lock — call body directly.
            pending = self.session.pending
            assert pending is not None
            for sid, _ in pairs:
                pending.acked.add(sid)
                for turn in self.session.history:
                    if turn.sentence_id == sid:
                        turn.spoken = True
            agreement = self._commit_pending(pending)
            self.session.pending = None
            result.agreement = agreement
        return result

    def _recent_public_turns(self) -> list[tuple[str, str]]:
        """Last ``NLG_CONTEXT_TURNS`` turns as ``(role, text)``, public lines only.

        Unspoken agent lines (barge-in) are skipped, and any line carrying a
        figure on the private blocklist is dropped, so the NLG prompt never sees
        a PRIVATE value even by coincidence.
        """
        session = self.session
        turns: list[tuple[str, list[str]]] = []
        for t in session.history:
            if t.role == "agent" and not t.spoken:
                continue
            if any(
                _cross_match(tok.kind, tok.value, session.private_blocklist)
                for tok in extract_tokens(t.text, ref=self._ref)
            ):
                continue
            if turns and turns[-1][0] == t.role:
                turns[-1][1].append(t.text)
            else:
                turns.append((t.role, [t.text]))
        return [(role, " ".join(lines)) for role, lines in turns[-NLG_CONTEXT_TURNS:]]

    async def _speak(self, action: Action, *, last_rep_line: str) -> list[str]:
        move = await self._speak_move(action, last_rep_line=last_rep_line)
        if not (action.ack or action.answer):
            return move
        return self._speak_acts(action) + move

    def _speak_acts(self, action: Action) -> list[str]:
        """H3 ack / answer sentences (no LLM call); blocks append to ``last_blocked``."""
        session = self.session
        return render_acts(
            action,
            self._ref,
            nlg_mode=self.settings.nlg_mode,
            bank_path=self.settings.nlg_bank_path,
            creditor_numbers=session.creditor_numbers,
            private_blocklist=session.private_blocklist,
            audit=self.audit,
            call_id=session.call_id,
            blocked_out=session.last_blocked,
            turn=session.neg.turn_idx,
            code_ack=not self.settings.nlg_h3,
        )

    async def _speak_move(self, action: Action, *, last_rep_line: str) -> list[str]:
        session = self.session
        session.last_blocked = []
        blocked_out = session.last_blocked
        trace_out: dict[str, Any] = {}
        self._trace_ctx["nlg"] = trace_out
        try:
            # bank mode needs no LLM; llm mode without a client degrades to TEMPLATES.
            mode = self.settings.nlg_mode
            if mode == "template" or (mode != "bank" and self.llm is None):
                return render_action(
                    action,
                    self._ref,
                    creditor_numbers=session.creditor_numbers,
                    private_blocklist=session.private_blocklist,
                    audit=self.audit,
                    call_id=session.call_id,
                    blocked_out=blocked_out,
                    trace_out=trace_out,
                )
            return await speak_action(
                action,
                self._ref,
                llm=self.llm,
                settings=self.settings,
                last_rep_line=last_rep_line,
                creditor_numbers=session.creditor_numbers,
                private_blocklist=session.private_blocklist,
                audit=self.audit,
                call_id=session.call_id,
                blocked_out=blocked_out,
                turn=session.neg.turn_idx,
                trace_out=trace_out,
                recent_turns=self._recent_public_turns() if last_rep_line else None,
            )
        except LLMUnavailable:
            self._audit("nlg", "llm_unavailable", {"fallback": SAFE_FALLBACK})
            trace_out = {}
            self._trace_ctx["nlg"] = trace_out
            spoken = render_action(
                action,
                self._ref,
                creditor_numbers=session.creditor_numbers,
                private_blocklist=session.private_blocklist,
                audit=self.audit,
                call_id=session.call_id,
                blocked_out=blocked_out,
                trace_out=trace_out,
            )
            trace_out["mode"] = self.settings.nlg_mode
            trace_out["fallback_used"] = True
            trace_out.setdefault("fallback_reason", "llm_unavailable")
            return spoken

    def _build_trace(
        self,
        action: Action,
        pairs: list[tuple[str, str]],
        timings: dict[str, float],
        belief_changes: list[BeliefChange],
    ) -> TurnTrace:
        """Assemble the decision trace from ``_trace_ctx`` and the emitted move.

        Contains PRIVATE data (``affordability``, guard ``offending``); the WS
        view filter strips it for the rep stream.
        """
        ctx = self._trace_ctx
        verified: VerifiedAnalysis | None = ctx.get("verified")
        afford: Affordability | None = ctx.get("afford")
        nlg: dict[str, Any] = ctx.get("nlg") or {}
        ask_bp: int | None = None
        if verified is not None and verified.ask_verified and verified.settlement_ask_pct:
            ask_bp = ask_pct_to_bp(verified.settlement_ask_pct)
        spoken_bp = action.facts.get("counter_pct") or action.facts.get("settlement_pct")
        return TurnTrace(
            turn=self.session.neg.turn_idx,
            creditor_text=ctx.get("creditor_text"),
            stance=verified.stance if verified is not None else None,  # type: ignore[arg-type]
            ask_bp=ask_bp,
            ask_quote=verified.ask_quote if ask_bp is not None and verified else None,
            terms=[
                TraceTerm.model_validate(t.model_dump(mode="json"))
                for t in (verified.terms if verified is not None else [])
            ],
            dropped=[
                DroppedTerm.model_validate(d.model_dump(mode="json"))
                for d in (verified.dropped if verified is not None else [])
            ],
            belief_changes=[
                TraceBeliefChange.model_validate(c.model_dump(mode="json"))
                for c in belief_changes
            ],
            affordability=(
                TraceAffordability(
                    max_bp=afford.max_bp,
                    curve=[
                        CurvePoint(bp=bp, feasible=ok)
                        for bp, ok in zip(_CURVE_BPS, afford.curve, strict=False)
                    ],
                )
                if afford is not None
                else None
            ),
            decide=Decide(
                intent=action.intent,
                reason=action.reason,
                reason_key=reason_key(action.intent, action.reason),
                reason_text=reason_text(action, self._ref),
            ),
            counter_bp=(
                spoken_bp.value
                if spoken_bp is not None and isinstance(spoken_bp.value, int)
                else None
            ),
            nlg=NlgTrace(
                mode=nlg.get("mode", "template"),
                source=nlg.get("source", "default"),
                template=nlg.get("template", ""),
                guards=[GuardResult.model_validate(g) for g in nlg.get("guards", [])],
                fallback_used=bool(nlg.get("fallback_used", False)),
                fallback_reason=nlg.get("fallback_reason"),
            ),
            spoken=[SpokenSentence(id=sid, text=text) for sid, text in pairs],
            timings={k: float(v) for k, v in timings.items() if isinstance(v, (int, float))},
        )

    def _apply_belief(
        self,
        verified: VerifiedAnalysis,
        turn: int,
        *,
        utterance: str = "",
        acked: dict[str, Any] | None = None,
    ) -> list[BeliefChange]:
        """Apply this turn's read-back answer and terms to belief.

        ``acked`` is what our previous line acknowledged. A correction of it
        ("No, it's six payments") replaces the value outright instead of the
        CONTRADICTED → CLARIFY path; a dispute with no value ("that's not what
        I said") puts the acked terms back to TENTATIVE so they are read back.
        """
        session = self.session
        changes: list[BeliefChange] = []
        acked = acked or {}
        correcting = bool(acked) and is_ack_correction(utterance)

        if session.neg.pending_readback and verified.readback_response in (
            "confirm",
            "deny",
        ):
            field = session.neg.pending_readback
            try:
                ch = session.belief.confirm_readback(
                    field, verified.readback_response == "confirm"
                )
                changes.append(ch)
                session.neg.pending_readback = None
                self._audit("belief", "confirm_readback", ch.model_dump(mode="json"))
            except ValueError:
                pass

        for term in verified.terms:
            cur = session.belief.get(term.field)
            if (
                correcting
                and term.field in acked
                and term.verified
                and not term.hedged
                and cur.status == TermStatus.KNOWN
                and cur.value == acked[term.field]
                and cur.value != term.value
            ):
                ch = session.belief.accept_alternative(term.field, term.value, term.quote, turn)
                changes.append(ch)
                self._audit("belief", "ack_corrected", ch.model_dump(mode="json"))
                continue
            # Post-proposal revision: accept new value without CONTRADICTED.
            if (
                session.neg.phase in (Phase.CONFIRM, Phase.NEGOTIATE)
                and (
                    verified.revises_terms
                    or verified.stance in ("offer", "counter", "reject")
                )
                and term.verified
                and not term.hedged
                and cur.status == TermStatus.KNOWN
                and cur.value != term.value
            ):
                ch = session.belief.accept_alternative(
                    term.field, term.value, term.quote, turn
                )
                changes.append(ch)
                self._audit("belief", "revise", ch.model_dump(mode="json"))
                continue
            ch = session.belief.observe(
                term.field,
                term.value,
                term.quote,
                turn,
                verified=term.verified,
                hedged=term.hedged,
            )
            changes.append(ch)
            self._audit("belief", "observe", ch.model_dump(mode="json"))

        if acked and is_ack_dispute(utterance):
            told = {t.field for t in verified.terms}
            for field, value in acked.items():
                cur = session.belief.get(field)
                if field in told or cur.status != TermStatus.KNOWN or cur.value != value:
                    continue
                ch = session.belief.reopen(field, utterance[:120], turn)
                changes.append(ch)
                self._audit("belief", "ack_disputed", ch.model_dump(mode="json"))

        return changes

    def _ingest_creditor_numbers(self, text: str) -> None:
        for tok in extract_tokens(text, ref=self._ref):
            self.session.creditor_numbers.add(tok.as_pair())

    def _engine_first_payment_date(self) -> date:
        """Belief's first payment date, or the engine default when it is unset.

        ``first_payment_date`` is not a required engine field, so ``build_rules``
        succeeds while it is UNKNOWN (a denied read-back clears the value). The
        engine then plans from the same EOM default the belief starts with as
        ASSUMED; the fallback is audited.
        """
        fpd = self.session.belief.get("first_payment_date").value
        if isinstance(fpd, date):
            return fpd
        default = default_first_payment_date(self.session.scenario.client)
        self._audit("engine", "first_payment_date_default", {"value": default.isoformat()})
        return default

    async def _eval_bp(self, bp: int) -> EvalSummary | None:
        """Evaluate schedule at ``bp`` under current belief; None if needs info."""
        session = self.session
        try:
            rules = build_rules(session.belief, session.scenario)
        except NeedsInfo:
            return None
        fpd = self._engine_first_payment_date()
        summary = await asyncio.to_thread(
            evaluate,
            session.scenario,
            rules,
            bp,
            fpd,
            assumed=assumed_fields(session.belief),
        )
        session.absorb_private_facts(summary)
        return summary

    async def _engine_context(
        self,
        verified: VerifiedAnalysis,
    ) -> tuple[Affordability | None, bool, tuple[str, Any] | None]:
        session = self.session
        try:
            rules = build_rules(session.belief, session.scenario)
        except NeedsInfo:
            self._audit(
                "engine",
                "needs_info",
                {"fields": session.belief.missing_required()},
            )
            return None, False, None

        fpd = self._engine_first_payment_date()

        afford = await asyncio.to_thread(affordability, session.scenario, rules, fpd)
        session.last_max_bp = afford.max_bp
        self._audit(
            "engine",
            "affordability",
            {"max_bp": afford.max_bp, "n_feasible": len(afford.feasible_bps)},
        )
        if afford.max_bp is not None:
            session.private_blocklist.add(("pct", afford.max_bp))

        ask_bp = session.neg.ask_bp
        if verified.settlement_ask_pct is not None and verified.ask_verified:
            ask_bp = ask_pct_to_bp(verified.settlement_ask_pct)

        rescue_ok = False
        term_alt: tuple[str, Any] | None = None

        # Empty curve, or ask above the ceiling: hunt non-price alts before
        # (re-)offering a stuck price counter.
        ask_unreachable = (
            ask_bp is not None
            and (
                afford.max_bp is None
                or ask_bp > afford.max_bp
                or ask_bp not in afford.feasible_bps
            )
        )
        if ask_unreachable:
            assert ask_bp is not None
            if afford.max_bp is None:
                summary = await asyncio.to_thread(
                    evaluate,
                    session.scenario,
                    rules,
                    ask_bp,
                    fpd,
                    assumed=assumed_fields(session.belief),
                )
                session.absorb_private_facts(summary)
                if summary.additional_funds is not None:
                    af = summary.additional_funds
                    rescue_ok = bool(
                        af.lump_sum.within_guardrail
                        or af.monthly_increment.within_guardrail
                    )
                self._audit(
                    "engine",
                    "rescue_check",
                    {"ask_bp": ask_bp, "within_guardrail": rescue_ok},
                )
            max_payments = int(session.belief.get("max_payments").value)
            min_payment_cents = int(session.belief.get("min_payment_cents").value)
            term_alt = await asyncio.to_thread(
                find_next_term_alt,
                session.scenario,
                rules,
                ask_bp=ask_bp,
                fpd=fpd,
                max_payments=max_payments,
                min_payment_cents=min_payment_cents,
                terms_countered=list(session.neg.terms_countered),
                baseline_max_bp=afford.max_bp,
            )
            self._audit(
                "engine",
                "term_alt",
                {
                    "requested_fpd": fpd.isoformat(),
                    "baseline_max_bp": afford.max_bp,
                    "alt_field": term_alt[0] if term_alt else None,
                    "alt_value": (
                        term_alt[1].isoformat()
                        if term_alt and isinstance(term_alt[1], date)
                        else (term_alt[1] if term_alt else None)
                    ),
                },
            )

        return afford, rescue_ok, term_alt

    async def _enrich_action(self, action: Action) -> Action:
        session = self.session
        if action.intent == Intent.SPEAK_SCHEDULE:
            summary = self._turn_eval or session.last_eval
            if summary is None or summary.rows is None:
                return action
            return speak_schedule_action(
                summary.rows,
                effects=list(action.effects),
                next_phase=action.next_phase,
                reason=action.reason,
            )

        if action.intent == Intent.CONFIRM_SCHEDULE:
            bp_fact = action.facts.get("settlement_pct")
            if bp_fact is None or not isinstance(bp_fact.value, int):
                return action
            bp = int(bp_fact.value)
            summary = await self._eval_bp(bp)
            if summary is None:
                return action
            self._turn_eval = summary
            self._turn_agreed_bp = bp
            facts = dict(action.facts)
            for fid, fact in summary.facts.public().items():
                facts[fid] = fact
            facts["settlement_pct"] = Fact(
                id="settlement_pct",
                kind="pct",
                value=bp,
                visibility="PUBLIC",
                source="engine",
            )
            required = set(facts.keys()) & {
                "offer_total",
                "num_payments",
                "first_payment_date",
                "settlement_pct",
            }
            return action.model_copy(update={"facts": facts, "required": required})

        if action.intent == Intent.PROPOSE_WRAP or (
            action.intent == Intent.CLOSE and action.reason == "thanks_accept"
        ):
            bp = session.neg.confirmed_bp
            if bp is None:
                return action
            summary = await self._eval_bp(bp)
            if summary is None:
                return action
            self._turn_eval = summary
            self._turn_agreed_bp = bp
            facts = dict(action.facts)
            for fid, fact in summary.facts.public().items():
                facts[fid] = fact
            facts["settlement_pct"] = Fact(
                id="settlement_pct",
                kind="pct",
                value=bp,
                visibility="PUBLIC",
                source="engine",
            )
            return action.model_copy(update={"facts": facts})

        if action.intent == Intent.COUNTER:
            bp_fact = action.facts.get("counter_pct")
            if bp_fact is not None and isinstance(bp_fact.value, int):
                bp = int(bp_fact.value)
                summary = await self._eval_bp(bp)
                if summary is not None:
                    self._turn_eval = summary
                    # So the schedule panel % matches the spoken counter.
                    self._turn_agreed_bp = bp
                    facts = dict(action.facts)
                    facts["offer_total"] = summary.facts["offer_total"]
                    return action.model_copy(
                        update={
                            "facts": facts,
                            "required": {"counter_pct", "offer_total"},
                        }
                    )
        return action

    def _maybe_draft_agreement(self, action: Action) -> Agreement | None:
        drafts = action.intent == Intent.PROPOSE_WRAP or (
            action.intent == Intent.CLOSE and action.reason == "thanks_accept"
        )
        if not drafts:
            return None
        session = self.session
        if session.last_eval is None or session.agreed_bp is None:
            self._audit("orchestrator", "wrap_missing_eval", {})
            return None
        summary = session.last_eval
        if not summary.feasible or summary.rows is None:
            self._audit("orchestrator", "wrap_infeasible", {})
            return None

        try:
            rules = build_rules(session.belief, session.scenario)
        except NeedsInfo as e:
            self._audit("orchestrator", "wrap_needs_info", {"fields": e.fields})
            return None
        fpd = self._engine_first_payment_date()
        violations = validate(
            summary.rows,
            session.scenario.client,
            summary.offer_total_cents,
            summary.program_fee_cents,
            rules,
            fpd,
        )
        self._audit(
            "orchestrator",
            "validator",
            {"violations": [v.rule for v in violations]},
        )
        if violations:
            return None

        agreement = draft_agreement(
            creditor=session.scenario.creditor,
            bp=session.agreed_bp,
            offer_total=summary.offer_total_cents,
            rows=summary.rows,
            assumed_fields=list(summary.assumed_fields),
            audit=self.audit,
            call_id=session.call_id,
        )
        session.agreement = agreement
        return agreement
