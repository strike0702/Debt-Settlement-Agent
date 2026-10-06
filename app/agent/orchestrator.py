"""Turn pipeline: NLU → belief → affordability → policy → NLG → speech ack.

``Orchestrator`` owns the per-session ``asyncio.Lock``, cancel-and-merge when
new creditor text arrives during NLU, and defers most ``Action.effects`` until
``on_sentence_done``. COUNTER / CONFIRM / COUNTER_TERMS bookkeeping
(``offer_counter``, ``record_confirm``, ``set_phase``, …) applies eagerly on
emit so a fast typed or voice accept wraps instead of re-confirming mid-TTS.
Barge-in still keeps those kinds when any sentence was heard; other pending
effects are dropped. Each public turn method runs inside
``llm_call_scope(call_id)`` so every NLU / NLG call is audited under this call.
Does not own policy rules or LLM prompts — those live in ``policy`` / ``nlu`` /
``nlg``.
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
from app.agent.nlg import SAFE_FALLBACK, render_action, speak_action
from app.agent.nlu import VerifiedAnalysis, analyze, try_resolve_cents_clarify
from app.agent.nlu_types import TurnAnalysis
from app.agent.numbers import extract_tokens
from app.agent.policy import (
    Action,
    Agreement,
    Effect,
    Intent,
    Phase,
    ask_pct_to_bp,
    cents_ambiguity_clarify_action,
    decide,
    draft_agreement,
    field_already_countered,
    opening_action,
    parse_pending_terms_value,
    speak_schedule_action,
    terms_counter_key,
)
from app.agent.session import CallSession, PendingSpeech, Turn
from app.config import Settings, get_settings
from app.domain.belief import BeliefChange, TermStatus
from app.domain.facts import Fact
from app.domain.scenario import CallScenario
from app.llm.call_audit import llm_call_scope
from app.llm.client import LLMUnavailable
from app.store.audit import AuditLog
from feasibility.models import CreditorRules, add_months, end_of_month


@dataclass
class Utterance:
    """Agent reply returned to CLI / WebSocket / tests."""

    sentences: list[tuple[str, str]]  # (sentence_id, text)
    action: Action
    timings: dict[str, float] = field(default_factory=dict)
    belief_changes: list[BeliefChange] = field(default_factory=list)
    agreement: Agreement | None = None


def _ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


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

    def _audit(self, actor: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        if self.audit is not None:
            self.audit.append(self.session.call_id, actor, event_type, payload)

    @_call_scoped
    async def start(self) -> Utterance:
        """Produce the OPENING line; effects apply on ack (or auto-ack)."""
        async with self._lock:
            t0 = time.perf_counter()
            action = opening_action(settings=self.settings)
            action = action.model_copy(
                update={
                    "effects": list(action.effects)
                    + [Effect(kind="set_phase", data={"phase": Phase.DISCOVERY.value})],
                }
            )
            sentences = await self._speak(action, last_rep_line="")
            timings = {
                "nlu_ms": 0.0,
                "policy_ms": 0.0,
                "nlg_ms": _ms_since(t0),
                "server_total_ms": _ms_since(t0),
            }
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

        Cancel-and-merge during NLU and ``_post_nlu_queue`` during post need
        concurrent ``on_creditor_text`` callers. The voice WS awaits each handler
        sequentially, so those paths are for in-process/tests; queued post-NLU
        text is auto-drained after speech ack (F08).
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

        while True:
            session.neg.turn_idx += 1
            turn = session.neg.turn_idx
            working = text

            session.history.append(Turn(role="creditor", text=working, spoken=True))
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
                if resolved is not None and resolved.terms:
                    verified = resolved
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
                else:
                    pending = session.neg.pending_cents_clarify
                    action = cents_ambiguity_clarify_action(
                        field=str(pending["field"]),
                        bare=int(pending["bare"]),
                    )
                    t_nlg = time.perf_counter()
                    sentences = await self._speak(action, last_rep_line=working)
                    timings["policy_ms"] = 0.0
                    timings["nlg_ms"] = _ms_since(t_nlg)
                    timings["server_total_ms"] = _ms_since(t_server)
                    return await self._emit(action, sentences, timings, [])

            if (
                verified.cents_ambiguity_bare is not None
                and verified.cents_ambiguity_field is not None
            ):
                action = cents_ambiguity_clarify_action(
                    field=verified.cents_ambiguity_field,
                    bare=verified.cents_ambiguity_bare,
                )
                # Eager: so a fast reply still resolves even if TTS ack is late.
                for eff in action.effects:
                    if eff.kind == "set_pending_cents_clarify":
                        apply_effects(session, [eff])
                t_nlg = time.perf_counter()
                sentences = await self._speak(action, last_rep_line=working)
                timings["policy_ms"] = 0.0
                timings["nlg_ms"] = _ms_since(t_nlg)
                timings["server_total_ms"] = _ms_since(t_server)
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

            belief_changes = self._apply_belief(verified, turn)
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
            afford, rescue_ok, term_alt = await self._engine_context(verified)

            t_pol = time.perf_counter()
            action = decide(
                session.belief,
                session.neg,
                verified.to_turn_analysis(),
                afford,
                settings=self.settings,
                rescue_within_guardrail=rescue_ok,
                term_alt=term_alt,
                accepted_term_alt=accepted_term_alt,
            )
            action = await self._enrich_action(action)
            timings["policy_ms"] = _ms_since(t_pol)
            self._audit(
                "policy",
                "decide",
                {
                    "intent": action.intent.value,
                    "reason": action.reason,
                    "next_phase": action.next_phase.value,
                    "policy_ms": timings["policy_ms"],
                },
            )

            t_nlg = time.perf_counter()
            sentences = await self._speak(action, last_rep_line=working)
            timings["nlg_ms"] = _ms_since(t_nlg)
            timings["server_total_ms"] = _ms_since(t_server)
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

    def _commit_pending(self, pending: PendingSpeech) -> Agreement | None:
        """Apply effects + deferred eval; draft agreement or fail WRAP to END."""
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
            self.session.neg.phase = Phase.END
            self._audit(
                "orchestrator",
                "wrap_failed_no_deal",
                {"reason": "draft_failed"},
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
            await self.on_creditor_text(queued, timings=None, oracle=drain_oracle)
        return agreement

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
        """Rep ended the chat; speak a short close and move to END."""
        async with self._lock:
            t0 = time.perf_counter()
            if self.session.neg.phase == Phase.WRAP:
                action = Action(
                    intent=Intent.CLOSE,
                    effects=[Effect(kind="set_phase", data={"phase": Phase.END.value})],
                    next_phase=Phase.END,
                    reason="rep_ended_after_wrap",
                )
            else:
                action = Action(
                    intent=Intent.NO_DEAL_WRAP,
                    text_slots={
                        "no_deal_reason": "Understood — we will end the call here."
                    },
                    effects=[Effect(kind="set_phase", data={"phase": Phase.END.value})],
                    next_phase=Phase.END,
                    reason="rep_ended",
                )
            sentences = await self._speak(action, last_rep_line="")
            timings = {
                "nlu_ms": 0.0,
                "policy_ms": 0.0,
                "nlg_ms": _ms_since(t0),
                "server_total_ms": _ms_since(t0),
            }
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

    async def _speak(self, action: Action, *, last_rep_line: str) -> list[str]:
        session = self.session
        session.last_blocked = []
        blocked_out = session.last_blocked
        try:
            if self.settings.nlg_mode == "template" or self.llm is None:
                return render_action(
                    action,
                    self._ref,
                    creditor_numbers=session.creditor_numbers,
                    private_blocklist=session.private_blocklist,
                    audit=self.audit,
                    call_id=session.call_id,
                    blocked_out=blocked_out,
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
            )
        except LLMUnavailable:
            self._audit("nlg", "llm_unavailable", {"fallback": SAFE_FALLBACK})
            return render_action(
                action,
                self._ref,
                creditor_numbers=session.creditor_numbers,
                private_blocklist=session.private_blocklist,
                audit=self.audit,
                call_id=session.call_id,
                blocked_out=blocked_out,
            )

    def _apply_belief(self, verified: VerifiedAnalysis, turn: int) -> list[BeliefChange]:
        session = self.session
        changes: list[BeliefChange] = []

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

        return changes

    def _ingest_creditor_numbers(self, text: str) -> None:
        for tok in extract_tokens(text, ref=self._ref):
            self.session.creditor_numbers.add(tok.as_pair())

    async def _eval_bp(self, bp: int) -> EvalSummary | None:
        """Evaluate schedule at ``bp`` under current belief; None if needs info."""
        session = self.session
        try:
            rules = build_rules(session.belief, session.scenario)
        except NeedsInfo:
            return None
        fpd = session.belief.get("first_payment_date").value
        assert isinstance(fpd, date)
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

        fpd = session.belief.get("first_payment_date").value
        assert isinstance(fpd, date)

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
        fpd = session.belief.get("first_payment_date").value
        assert isinstance(fpd, date)
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
