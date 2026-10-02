"""Turn pipeline: NLU → belief → affordability → policy → NLG → speech ack.

``Orchestrator`` owns the per-session ``asyncio.Lock``, cancel-and-merge when
new creditor text arrives during NLU, and defers ``Action.effects`` until
``on_sentence_done``. Barge-in drops pending effects but keeps belief updates.
Does not own policy rules or LLM prompts — those live in ``policy`` / ``nlu`` /
``nlg``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

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
from app.agent.nlu import VerifiedAnalysis, analyze
from app.agent.nlu_types import TurnAnalysis
from app.agent.numbers import extract_tokens
from app.agent.policy import (
    Action,
    Agreement,
    Effect,
    Intent,
    Phase,
    ask_pct_to_bp,
    decide,
    draft_agreement,
    next_counter,
    opening_action,
    speak_schedule_action,
)
from app.agent.session import CallSession, PendingSpeech, Turn
from app.config import Settings, get_settings
from app.domain.belief import BeliefChange
from app.domain.facts import Fact
from app.domain.scenario import CallScenario
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
    """Scan EOM candidates within the savings horizon; pick closest feasible.

    Prefers dates where ``ask_bp`` is feasible; otherwise any non-empty
    affordability curve. Never re-offers an ISO date in ``already``.
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
    best_dist: int | None = None
    for cand in candidates:
        iso = cand.isoformat()
        if iso in already:
            continue
        aff = affordability(scenario, rules, cand)
        if aff.max_bp is None:
            continue
        if ask_bp is not None and ask_bp not in aff.feasible_bps:
            # Still usable if *something* is feasible; prefer ask-feasible first.
            ask_ok = False
        else:
            ask_ok = True
        dist = abs((cand - requested).days)
        # Prefer ask-feasible; among equals, closest to requested.
        rank_dist = dist if ask_ok else dist + 100_000
        if best is None or best_dist is None or rank_dist < best_dist:
            best = cand
            best_dist = rank_dist
    return best


def apply_effects(session: CallSession, effects: list[Effect]) -> None:
    """Commit deferred negotiation side effects (speech ack path only)."""
    for effect in effects:
        kind = effect.kind
        data = effect.data
        if kind == "offer_counter":
            session.neg.counters_offered.append(int(data["bp"]))
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
        elif kind == "inc_reject_at_max":
            session.neg.rejects += 1
        elif kind == "record_confirm":
            raw_key = data.get("key")
            if isinstance(raw_key, list):
                session.neg.last_confirm_key = tuple(raw_key)
            elif isinstance(raw_key, tuple):
                session.neg.last_confirm_key = raw_key
            else:
                session.neg.last_confirm_key = (int(data["ask_bp"]),)
        elif kind == "inc_confirm_reject":
            session.neg.confirm_rejects += 1
        elif kind == "note_assumed_asked":
            session.neg.assumed_asked.add(str(data["field"]))
        elif kind == "note_clarify":
            fname = str(data["field"])
            session.neg.clarify_counts[fname] = session.neg.clarify_counts.get(fname, 0) + 1
        elif kind == "note_terms_countered":
            iso = str(data["value"])
            if iso not in session.neg.terms_countered:
                session.neg.terms_countered.append(iso)
            session.neg.pending_terms_alt = iso
        elif kind == "clear_pending_terms_alt":
            session.neg.pending_terms_alt = None
        elif kind == "set_phase":
            session.neg.phase = Phase(str(data["phase"]))


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

    async def on_creditor_text(
        self,
        text: str,
        timings: dict[str, float] | None = None,
        *,
        oracle: TurnAnalysis | None = None,
    ) -> Utterance:
        """NLU → belief → afford → decide → NLG. Cancel-and-merge during NLU."""
        out_timings = timings if timings is not None else {}

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
                    "nlu_ms": timings["nlu_ms"],
                },
            )

            belief_changes = self._apply_belief(verified, turn)
            session.last_belief_changes = belief_changes

            # Accept a pending non-price alternative before affordability.
            if (
                session.neg.pending_terms_alt is not None
                and verified.stance == "accept"
            ):
                alt = date.fromisoformat(session.neg.pending_terms_alt)
                ch = session.belief.accept_alternative(
                    "first_payment_date",
                    alt,
                    "accepted alternate start",
                    turn,
                )
                belief_changes.append(ch)
                self._audit("belief", "accept_terms_alt", ch.model_dump(mode="json"))
                session.neg.pending_terms_alt = None

            self._turn_eval = None
            self._turn_agreed_bp = None
            afford, confirm_facts, counter_total, rescue_ok, alt_fpd = (
                await self._engine_context(verified)
            )

            t_pol = time.perf_counter()
            action = decide(
                session.belief,
                session.neg,
                verified.to_turn_analysis(),
                afford,
                settings=self.settings,
                rescue_within_guardrail=rescue_ok,
                confirm_facts=confirm_facts,
                counter_offer_total_cents=counter_total,
                alt_first_payment_date=alt_fpd,
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

    async def on_sentence_done(self, ids: list[str] | set[str]) -> Agreement | None:
        """Ack spoken sentence ids; commit effects when all pending are done."""
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
            return agreement

    async def on_barge_in(self, spoken_ids: list[str] | set[str]) -> None:
        """Keep spoken ids; drop unspoken sentences and pending effects."""
        async with self._lock:
            pending = self.session.pending
            spoken = set(spoken_ids)
            if pending is None:
                self._audit("orchestrator", "barge_in", {"spoken_ids": list(spoken)})
                return

            for turn in self.session.history:
                if turn.sentence_id is not None and turn.sentence_id in pending.sentence_ids:
                    turn.spoken = turn.sentence_id in spoken

            dropped = pending.action
            self.session.pending = None
            self._audit(
                "orchestrator",
                "barge_in",
                {
                    "spoken_ids": list(spoken),
                    "dropped_intent": dropped.intent.value,
                    "dropped_effects": [e.model_dump() for e in dropped.effects],
                },
            )

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
        # Eager COUNTER_TERMS pending so accept can apply before ack.
        if action.intent == Intent.COUNTER_TERMS:
            fact = action.facts.get("alt_first_payment_date")
            if fact is not None and isinstance(fact.value, date):
                iso = fact.value.isoformat()
                self.session.neg.pending_terms_alt = iso
                if iso not in self.session.neg.terms_countered:
                    self.session.neg.terms_countered.append(iso)
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

    async def _engine_context(
        self,
        verified: VerifiedAnalysis,
    ) -> tuple[
        Affordability | None,
        dict[str, Fact] | None,
        int | None,
        bool,
        date | None,
    ]:
        session = self.session
        try:
            rules = build_rules(session.belief, session.scenario)
        except NeedsInfo:
            self._audit(
                "engine",
                "needs_info",
                {"fields": session.belief.missing_required()},
            )
            return None, None, None, False, None

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

        confirm_facts: dict[str, Fact] | None = None
        counter_total: int | None = None
        rescue_ok = False
        alt_fpd: date | None = None

        if ask_bp is not None and afford.max_bp is None:
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
                    af.lump_sum.within_guardrail or af.monthly_increment.within_guardrail
                )
            self._audit(
                "engine",
                "rescue_check",
                {"ask_bp": ask_bp, "within_guardrail": rescue_ok},
            )
            if not rescue_ok:
                alt_fpd = await asyncio.to_thread(
                    find_alt_first_payment_date,
                    session.scenario,
                    rules,
                    ask_bp=ask_bp,
                    requested=fpd,
                    already=set(session.neg.terms_countered),
                )
                self._audit(
                    "engine",
                    "alt_first_payment_date",
                    {
                        "requested": fpd.isoformat(),
                        "alt": alt_fpd.isoformat() if alt_fpd else None,
                    },
                )

        if ask_bp is not None and afford.max_bp is not None:
            if ask_bp <= afford.max_bp and ask_bp in afford.feasible_bps:
                summary = await asyncio.to_thread(
                    evaluate,
                    session.scenario,
                    rules,
                    ask_bp,
                    fpd,
                    assumed=assumed_fields(session.belief),
                )
                self._turn_eval = summary
                self._turn_agreed_bp = ask_bp
                session.absorb_private_facts(summary)
                confirm_facts = dict(summary.facts.public())
                confirm_facts["settlement_pct"] = Fact(
                    id="settlement_pct",
                    kind="pct",
                    value=ask_bp,
                    visibility="PUBLIC",
                    source="engine",
                )
            elif verified.stance == "accept" and session.neg.counters_offered:
                bp = session.neg.counters_offered[-1]
                summary = await asyncio.to_thread(
                    evaluate,
                    session.scenario,
                    rules,
                    bp,
                    fpd,
                    assumed=assumed_fields(session.belief),
                )
                self._turn_eval = summary
                self._turn_agreed_bp = bp
                session.absorb_private_facts(summary)
                confirm_facts = dict(summary.facts.public())
                confirm_facts["settlement_pct"] = Fact(
                    id="settlement_pct",
                    kind="pct",
                    value=bp,
                    visibility="PUBLIC",
                    source="engine",
                )

        if (
            ask_bp is not None
            and afford.max_bp is not None
            and not (ask_bp <= afford.max_bp and ask_bp in afford.feasible_bps)
            and verified.stance != "accept"
            and not session.belief.missing_required()
            and not session.belief.tentative_fields()
            and not session.belief.contradicted_fields()
        ):
            c_prev = (
                session.neg.counters_offered[-1] if session.neg.counters_offered else None
            )
            try:
                c_next = next_counter(
                    ask_bp=ask_bp,
                    max_bp=afford.max_bp,
                    feasible_bps=afford.feasible_bps,
                    c_prev=c_prev,
                    anchor_ratio=self.settings.anchor_ratio,
                    concession_factor=self.settings.concession_factor,
                )
            except ValueError:
                c_next = None
            if c_next is not None and c_next < ask_bp:
                summary = await asyncio.to_thread(
                    evaluate,
                    session.scenario,
                    rules,
                    c_next,
                    fpd,
                    assumed=assumed_fields(session.belief),
                )
                session.absorb_private_facts(summary)
                counter_total = summary.offer_total_cents

        return afford, confirm_facts, counter_total, rescue_ok, alt_fpd

    async def _enrich_action(self, action: Action) -> Action:
        turn_eval = self._turn_eval
        turn_bp = self._turn_agreed_bp
        if action.intent == Intent.SPEAK_SCHEDULE:
            summary = turn_eval or self.session.last_eval
            if summary is None or summary.rows is None:
                return action
            return speak_schedule_action(
                summary.rows,
                effects=list(action.effects),
                next_phase=action.next_phase,
                reason=action.reason,
            )

        if action.intent == Intent.CONFIRM_SCHEDULE and turn_eval is not None:
            facts = dict(action.facts)
            for fid, fact in turn_eval.facts.public().items():
                facts.setdefault(fid, fact)
            if turn_bp is not None:
                facts.setdefault(
                    "settlement_pct",
                    Fact(
                        id="settlement_pct",
                        kind="pct",
                        value=turn_bp,
                        visibility="PUBLIC",
                        source="engine",
                    ),
                )
            required = set(facts.keys()) & {
                "offer_total",
                "num_payments",
                "first_payment_date",
            }
            return action.model_copy(update={"facts": facts, "required": required})

        if action.intent == Intent.COUNTER and "offer_total" not in action.facts:
            bp_fact = action.facts.get("counter_pct")
            if bp_fact is not None and isinstance(bp_fact.value, int):
                try:
                    rules = build_rules(self.session.belief, self.session.scenario)
                    fpd = self.session.belief.get("first_payment_date").value
                    assert isinstance(fpd, date)
                    summary = await asyncio.to_thread(
                        evaluate,
                        self.session.scenario,
                        rules,
                        bp_fact.value,
                        fpd,
                        assumed=assumed_fields(self.session.belief),
                    )
                    self.session.absorb_private_facts(summary)
                    facts = dict(action.facts)
                    facts["offer_total"] = summary.facts["offer_total"]
                    return action.model_copy(
                        update={
                            "facts": facts,
                            "required": {"counter_pct", "offer_total"},
                        }
                    )
                except NeedsInfo:
                    pass
        return action

    def _maybe_draft_agreement(self, action: Action) -> Agreement | None:
        if action.intent == Intent.PROPOSE_WRAP:
            pass
        elif action.intent == Intent.CLOSE and action.reason == "thanks_accept":
            pass
        else:
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
