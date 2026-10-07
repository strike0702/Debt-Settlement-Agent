"""WebSocket ``/ws/call/{call_id}?view=rep|operator`` protocol for the voice UI.

Client events: ``start``, ``end``, binary WAV, ``text``, ``sentence_done``,
``barge_in``, ``timing``. Server events (every ``type`` this module sends):
``transcript``, ``say``, ``belief``, ``eval``, ``blocked``, ``escalate``,
``latency``, ``audit``, ``phase``, ``agreement``, ``stt_error``, ``error``,
``turn_done``, ``turn_trace`` (one per agent turn, operator only; it gains
``needs_info`` when the engine did not run), ``autoplay_done``. Their
Pydantic models live in ``app.schemas.events``. Speaks through
``Orchestrator``; STT via ``app.voice.stt``, run inside
``llm_call_scope(call_id)`` so the STT call is audited.

Every frame passes ``app.voice.views.redact_for_view``: ``view=operator`` (the
default) sees everything, ``view=rep`` never sees the decision trace, fees,
balances, rescue data or private audit rows. ``start`` with
``"autoplay": true`` runs ``app.autoplay`` (sim creditor, no LLM) over the same
events; client text / audio is refused while it runs. An NLU or STT
``LLMUnavailable`` answers ``error`` + ``turn_done`` and keeps the socket open.

Whatever the socket's view, every ``turn_trace`` / ``audit`` / ``eval`` /
``agreement`` frame is also kept, as the operator stream would see it, in a
bounded in-memory store per call (``operator_detail``, served by
``GET /calls/{id}/operator``), so the Debt negotiator view can show the
decision trace of a call that streams the Creditor rep view. The store never
feeds the rep socket.

A reader task feeds an ``asyncio.Queue`` and each event (except ``start``)
runs as its own task, so ``text`` / ``barge_in`` / ``sentence_done`` are
handled while a turn awaits NLU: the orchestrator's cancel-and-merge is live.
``latency`` carries ``stt_ms``, ``nlu_ms``, ``engine_ms``, ``policy_ms``,
``nlg_ms``, ``queue_ms`` (limiter wait inside the other stages, not additive)
and ``server_total_ms``. Does not own policy or NLG.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.adapter.engine_adapter import EvalSummary
from app.agent.nlu_types import TurnAnalysis
from app.agent.orchestrator import Orchestrator, Utterance
from app.agent.policy import Agreement, Intent
from app.agent.session import CallSession
from app.autoplay import clamp_pause_ms, new_autoplay_call, run_autoplay
from app.config import Settings, get_settings
from app.domain.fields import REQUIRED_FIELDS
from app.domain.scenario import (
    CallScenario,
    load_scenario,
    resolve_scenario_dir,
    scenario_from_payload,
)
from app.llm.call_audit import llm_call_scope
from app.llm.client import LLMUnavailable, queue_wait_scope
from app.schemas.events import VIEWS, View
from app.store.audit import AuditLog
from app.voice.metrics_buf import LATENCY_BUFFER
from app.voice.stt import transcribe
from app.voice.views import redact_for_view

router = APIRouter()

# Injected by ``app.main`` lifespan (shared across connections).
_audit: AuditLog | None = None
_llm: Any | None = None
_settings: Settings | None = None


def configure(
    *,
    audit: AuditLog,
    llm: Any,
    settings: Settings | None = None,
) -> None:
    """Bind process-level audit + LLM used by new call sockets."""
    global _audit, _llm, _settings
    _audit = audit
    _llm = llm
    _settings = settings or get_settings()


def _ser_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    return value


def _belief_payload(session: CallSession) -> dict[str, Any]:
    terms = []
    for name, term in session.belief.terms.items():
        terms.append(
            {
                "field": name,
                "value": _ser_value(term.value),
                "status": term.status.value,
                "evidence": [
                    {"turn": e.turn, "quote": e.quote} for e in term.evidence
                ],
                "history": [_ser_value(h) for h in term.history],
            }
        )
    return {"type": "belief", "terms": terms}


def _serialize_rows(summary: EvalSummary) -> list[dict[str, Any]] | None:
    if summary.rows is None:
        return None
    return [
        {
            "date": r.date.isoformat(),
            "creditor_payment_cents": r.creditor_payment_cents,
            "program_fee_cents": r.program_fee_cents,
            "bank_fee_cents": r.bank_fee_cents,
            "balance_cents": r.balance_cents,
        }
        for r in summary.rows
    ]


def _serialize_additional(summary: EvalSummary) -> dict[str, Any] | None:
    af = summary.additional_funds
    if af is None:
        return None

    def _opt(o: Any) -> dict[str, Any]:
        return {
            "amount_cents": o.amount_cents,
            "within_guardrail": o.within_guardrail,
            "reason": o.reason,
            "date": o.date.isoformat() if o.date is not None else None,
            "num_drafts": o.num_drafts,
        }

    return {
        "lump_sum": _opt(af.lump_sum),
        "monthly_increment": _opt(af.monthly_increment),
    }


def _eval_payload(session: CallSession, summary: EvalSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    # Prefer the bp tied to this turn's pending eval (COUNTER/CONFIRM) over a
    # stale session.agreed_bp so the schedule header matches spoken offer.
    agreed_bp: int | None = None
    if session.pending is not None and session.pending.pending_agreed_bp is not None:
        agreed_bp = session.pending.pending_agreed_bp
    elif session.agreed_bp is not None:
        agreed_bp = session.agreed_bp
    return {
        "type": "eval",
        "feasible": summary.feasible,
        "shape": summary.shape,
        "offer_total_cents": summary.offer_total_cents,
        "program_fee_cents": summary.program_fee_cents,
        "assumed_fields": list(summary.assumed_fields),
        "agreed_bp": agreed_bp,
        "rows": _serialize_rows(summary),
        "additional_funds": _serialize_additional(summary),
        "max_bp": session.last_max_bp,
    }


def _phase_payload(session: CallSession, intent: str | None = None) -> dict[str, Any]:
    return {
        "type": "phase",
        "phase": session.neg.phase.value,
        "intent": intent,
        "turn": session.neg.turn_idx,
    }


# Debt negotiator detail per call (Phase 36). Operator data, like the rest of a
# call's in-memory state: kept whatever view the socket streams, oldest call
# evicted first, lost on restart (the UI then says the trace is gone).
_DETAIL_KINDS = frozenset({"turn_trace", "audit", "eval", "agreement"})
_DETAIL_MAX_CALLS = 256
_DETAIL: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _reset_operator_detail(call_id: str) -> None:
    """Start an empty record for ``call_id`` (a new ``start`` replaces the old one)."""
    _DETAIL.pop(call_id, None)
    _DETAIL[call_id] = {"traces": {}, "audit": {}, "eval": None, "agreement": None}
    while len(_DETAIL) > _DETAIL_MAX_CALLS:
        _DETAIL.popitem(last=False)


def _record_operator_detail(call_id: str, payload: dict[str, Any]) -> None:
    if payload.get("type") not in _DETAIL_KINDS or call_id not in _DETAIL:
        return
    out = redact_for_view(payload, "operator")
    if out is None:
        return
    detail = _DETAIL[call_id]
    kind = out["type"]
    if kind == "turn_trace":
        detail["traces"][out["turn"]] = out
    elif kind == "audit":
        detail["audit"][out["id"]] = out
    else:
        detail[kind] = out


def operator_detail(call_id: str) -> dict[str, Any] | None:
    """``{call_id, traces, audit, eval, agreement}`` as the operator stream saw them, or None."""
    detail = _DETAIL.get(call_id)
    if detail is None:
        return None
    return {
        "call_id": call_id,
        "traces": [detail["traces"][t] for t in sorted(detail["traces"])],
        "audit": list(detail["audit"].values()),
        "eval": detail["eval"],
        "agreement": detail["agreement"],
    }


class _ViewSocket:
    """The accepted socket, with every outgoing frame filtered for ``view``.

    Also records the operator detail of each frame for ``call_id`` (see above).
    """

    def __init__(self, ws: WebSocket, view: View, call_id: str = "") -> None:
        self.ws = ws
        self.view = view
        self.call_id = call_id

    async def send_json(self, payload: dict[str, Any]) -> None:
        _record_operator_detail(self.call_id, payload)
        out = redact_for_view(payload, self.view)
        if out is not None:
            await self.ws.send_json(out)

    async def receive(self) -> Any:
        return await self.ws.receive()


async def _send(ws: _ViewSocket | WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_json(payload)


def _agreement_payload(agreement: Agreement) -> dict[str, Any]:
    return {
        "type": "agreement",
        "creditor": agreement.creditor,
        "bp": agreement.bp,
        "offer_total": agreement.offer_total,
        "status": agreement.status,
        "assumed_fields": list(agreement.assumed_fields),
        "rows": agreement.rows,
    }


async def _send_audit_tail(
    ws: _ViewSocket,
    audit: AuditLog,
    call_id: str,
    *,
    after_id: int,
) -> int:
    """Push new audit rows; return the latest id seen."""
    latest = after_id
    for row in audit.for_call(call_id):
        rid = int(row["id"])
        if rid <= after_id:
            continue
        await _send(
            ws,
            {
                "type": "audit",
                "id": rid,
                "ts": row["ts"],
                "actor": row["actor"],
                "event": row["type"],
                "payload": row["payload"],
            },
        )
        latest = rid
    return latest


async def _send_creditor_transcript(ws: _ViewSocket, text: str) -> None:
    """Push the rep line as soon as STT/text is known (before NLU/NLG)."""
    await _send(
        ws,
        {
            "type": "transcript",
            "role": "creditor",
            "text": text,
            "spoken": True,
            "sentence_id": None,
            "blocked": False,
        },
    )


_LATENCY_KEYS = (
    "stt_ms",
    "nlu_ms",
    "engine_ms",
    "policy_ms",
    "nlg_ms",
    "queue_ms",
    "server_total_ms",
)


def _missing_engine_fields(session: CallSession) -> list[str]:
    """Required engine fields the belief cannot use yet (``build_rules``'s NeedsInfo list).

    Read after the turn, so on an auto-acked turn it reflects committed effects;
    an empty list means the engine was skipped for another reason (e.g. a
    dollars-vs-cents clarify), not for missing rules.
    """
    return [f for f in REQUIRED_FIELDS if not session.belief.usable_for_engine(f)]


async def _emit_utterance(
    ws: _ViewSocket,
    orch: Orchestrator,
    utt: Utterance,
    *,
    creditor_text: str | None,
    stt_ms: float | None,
    audit_after: int,
    stt_queue_ms: float = 0.0,
) -> int:
    session = orch.session
    blocked = bool(session.last_blocked)

    # Prefer callers that already emitted via ``_send_creditor_transcript``.
    if creditor_text is not None:
        await _send_creditor_transcript(ws, creditor_text)

    for sid, text in utt.sentences:
        await _send(ws, {"type": "say", "id": sid, "text": text})
        await _send(
            ws,
            {
                "type": "transcript",
                "role": "agent",
                "text": text,
                "spoken": False,
                "sentence_id": sid,
                "blocked": blocked,
            },
        )

    await _send(ws, _belief_payload(session))
    await _send(ws, _phase_payload(session, utt.action.intent.value))

    eval_src = (
        session.pending.pending_eval
        if session.pending is not None and session.pending.pending_eval is not None
        else session.last_eval
    )
    ev = _eval_payload(session, eval_src)
    if ev is not None:
        await _send(ws, ev)

    for entry in session.last_blocked:
        await _send(ws, {"type": "blocked", **entry})

    if utt.action.intent == Intent.ESCALATE:
        await _send(
            ws,
            {
                "type": "escalate",
                "reason": utt.action.reason,
                "escalate_reason": utt.action.text_slots.get("escalate_reason"),
            },
        )

    timings = dict(utt.timings)
    if stt_ms is not None:
        timings["stt_ms"] = stt_ms
        # queue_ms covers every limiter wait of the turn, STT included.
        timings["queue_ms"] = float(timings.get("queue_ms") or 0.0) + stt_queue_ms
    latency = {
        "type": "latency",
        "turn": session.neg.turn_idx,
        **{k: timings.get(k) for k in _LATENCY_KEYS},
    }
    await _send(ws, latency)
    LATENCY_BUFFER.record(
        {k: float(v) for k, v in timings.items() if isinstance(v, (int, float))}
    )

    if utt.trace is not None:
        trace = utt.trace.model_dump(mode="json")
        # The trace was closed before STT was known; same view as ``latency``.
        trace["timings"] = {k: v for k, v in timings.items() if isinstance(v, (int, float))}
        if trace.get("affordability") is None and trace.get("creditor_text") is not None:
            trace["needs_info"] = _missing_engine_fields(session)
        await _send(ws, {"type": "turn_trace", **trace})
    # Auto-ack calls (autoplay) commit on emit, so the agreement belongs to this turn.
    if orch.auto_ack and utt.agreement is not None:
        await _send(ws, _agreement_payload(utt.agreement))

    if orch.audit is not None:
        audit_after = await _send_audit_tail(
            ws, orch.audit, session.call_id, after_id=audit_after
        )
    await _send(ws, {"type": "turn_done"})
    return audit_after


def _parse_oracle(raw: Any) -> TurnAnalysis | None:
    if raw is None:
        return None
    if isinstance(raw, TurnAnalysis):
        return raw
    try:
        return TurnAnalysis.model_validate(raw)
    except ValidationError:
        return None


def _load_start_scenario(data: dict[str, Any]) -> tuple[CallScenario, str | None, str | None]:
    """Resolve the ``start`` payload to ``(scenario, scenario_id, rebased_as_of)``.

    Raises ``_StartError`` for a user-facing error message.
    """
    scenario_id = data.get("scenario_id")
    custom = data.get("scenario_payload")
    try:
        if isinstance(custom, dict):
            scenario = scenario_from_payload(
                custom,
                scenario_id=str(scenario_id or "custom"),
                rebase_to=date.today(),
            )
            return scenario, scenario.id, scenario.client.as_of_date.isoformat()
        if scenario_id:
            path = resolve_scenario_dir(str(scenario_id))
            scenario = load_scenario(path, rebase_to=date.today())
            return scenario, scenario_id, scenario.client.as_of_date.isoformat()
        # Legacy ``start.scenario`` path (fixtures/demo) for unit tests.
        # UI sends ``scenario_id`` only (F26).
        path = Path(data.get("scenario") or "fixtures/demo")
        if not path.is_dir():
            raise _StartError(f"scenario not found: {path}")
        # Only allow under fixtures/
        resolved = path.resolve()
        fixtures_root = Path("fixtures").resolve()
        if fixtures_root not in resolved.parents and resolved != fixtures_root:
            raise _StartError("scenario path denied")
        scenario = load_scenario(path)
        return scenario, scenario.id, None
    except (ValueError, FileNotFoundError, TypeError, KeyError) as e:
        raise _StartError(str(e)) from e


class _StartError(Exception):
    """``start`` payload could not be turned into a scenario."""


_DISCONNECT = object()
_AUTOPLAY_BUSY = {"type": "error", "message": "autoplay is driving this call"}


class _CallConnection:
    """One socket: a reader task feeds ``_inbox``; each event runs as its own task.

    Running handlers concurrently is what makes the orchestrator's
    cancel-and-merge live: a second ``text`` (or ``barge_in`` /
    ``sentence_done``) is handled while the first turn awaits NLU. ``start``
    runs inline so no event reaches a call that does not exist yet. Every
    multi-message emission holds ``_send_lock`` so frames from different
    handlers never interleave, and ``audit_after`` only advances under it.
    """

    def __init__(
        self, ws: _ViewSocket, call_id: str, *, audit: AuditLog, llm: Any, settings: Settings
    ) -> None:
        self.ws = ws
        self.call_id = call_id
        self.audit = audit
        self.llm = llm
        self.settings = settings
        self.orch: Orchestrator | None = None
        self.audit_after = 0
        self._send_lock = asyncio.Lock()
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        # Merged / queued text callers get the in-flight turn's Utterance back;
        # remember recent ones so each is emitted once.
        self._emitted: deque[Utterance] = deque(maxlen=16)
        # Set while a sim creditor drives the call (``start`` with ``autoplay``).
        self._autoplay: asyncio.Task[None] | None = None

    async def run(self) -> None:
        reader = asyncio.create_task(self._read())
        try:
            while True:
                item = await self._inbox.get()
                if item is _DISCONNECT:
                    return
                if isinstance(item, BaseException):
                    raise item
                await self._dispatch(item)
        finally:
            reader.cancel()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(reader, *self._tasks, return_exceptions=True)

    async def _read(self) -> None:
        while True:
            message = await self.ws.receive()
            if message.get("type") == "websocket.disconnect":
                await self._inbox.put(_DISCONNECT)
                return
            await self._inbox.put(message)

    def _spawn(self, coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    @property
    def autoplaying(self) -> bool:
        return self._autoplay is not None and not self._autoplay.done()

    async def _stop_autoplay(self) -> None:
        task, self._autoplay = self._autoplay, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Surface handler failures on the main loop (audited + re-raised there).
            self._inbox.put_nowait(exc)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if message.get("bytes") is not None:
            if self.orch is None:
                await self._send_one({"type": "error", "message": "call not started"})
                return
            if self.autoplaying:
                await self._send_one(_AUTOPLAY_BUSY)
                return
            self._spawn(self._on_wav(message["bytes"]))
            return

        raw_text = message.get("text")
        if raw_text is None:
            return
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            await self._send_one({"type": "error", "message": "invalid json"})
            return

        event = data.get("type")
        if event == "start":
            await self._on_start(data)
            return
        if self.orch is None:
            await self._send_one({"type": "error", "message": "call not started"})
            return
        if self.autoplaying:
            if event == "text":
                await self._send_one(_AUTOPLAY_BUSY)
                return
            if event in ("sentence_done", "barge_in"):
                return  # autoplay auto-acks; client TTS acks are moot
            if event == "end":
                await self._stop_autoplay()
        handler = {
            "end": self._on_end,
            "text": self._on_text,
            "sentence_done": self._on_sentence_done,
            "barge_in": self._on_barge_in,
            "timing": self._on_timing,
        }.get(event)
        if handler is None:
            await self._send_one({"type": "error", "message": f"unknown event: {event}"})
            return
        self._spawn(handler(data))

    async def _send_one(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await _send(self.ws, payload)

    async def _audit_tail_and_done(self) -> None:
        """Push new audit rows then ``turn_done``; caller holds ``_send_lock``."""
        self.audit_after = await _send_audit_tail(
            self.ws, self.audit, self.call_id, after_id=self.audit_after
        )
        await _send(self.ws, {"type": "turn_done"})

    async def _emit(
        self, utt: Utterance, *, stt_ms: float | None = None, stt_queue_ms: float = 0.0
    ) -> None:
        """Emit ``utt`` once; a caller that got an already-emitted one only gets ``turn_done``."""
        assert self.orch is not None
        if any(u is utt for u in self._emitted):
            await self._send_one({"type": "turn_done"})
            return
        self._emitted.append(utt)
        async with self._send_lock:
            self.audit_after = await _emit_utterance(
                self.ws,
                self.orch,
                utt,
                creditor_text=None,
                stt_ms=stt_ms,
                audit_after=self.audit_after,
                stt_queue_ms=stt_queue_ms,
            )

    async def _emit_drained(self) -> None:
        assert self.orch is not None
        for utt in self.orch.pop_drained():
            await self._emit(utt)

    async def _nlu_unavailable(self, e: LLMUnavailable) -> None:
        """No NLU provider answered: tell the client, unblock it, keep the call alive."""
        self.audit.append(self.call_id, "nlu", "llm_unavailable", {"message": str(e)})
        async with self._send_lock:
            await _send(self.ws, {"type": "error", "message": f"NLU unavailable: {e}"})
            await self._audit_tail_and_done()

    async def _on_start(self, data: dict[str, Any]) -> None:
        await self._stop_autoplay()
        autoplay = bool(data.get("autoplay"))
        if autoplay and not (data.get("scenario_id") and data.get("scenario_payload") is None):
            await self._send_one(
                {"type": "error", "message": "autoplay needs a curated scenario_id"}
            )
            return
        try:
            scenario, scenario_id, rebased_as_of = _load_start_scenario(data)
        except _StartError as e:
            await self._send_one({"type": "error", "message": str(e)})
            return
        _reset_operator_detail(self.call_id)
        if autoplay:
            await self._start_autoplay(data, scenario, str(scenario_id), rebased_as_of)
            return
        session = CallSession(scenario=scenario, call_id=self.call_id)
        self.orch = Orchestrator(
            session,
            llm=self.llm,
            settings=self.settings,
            audit=self.audit,
            auto_ack=False,
        )
        self.audit.append(
            self.call_id,
            "orchestrator",
            "call_started",
            {"scenario_id": scenario_id, "rebased_as_of": rebased_as_of},
        )
        utt = await self.orch.start()
        self.audit_after = 0
        await self._emit(utt)

    async def _start_autoplay(
        self,
        data: dict[str, Any],
        scenario: CallScenario,
        scenario_id: str,
        rebased_as_of: str | None,
    ) -> None:
        try:
            orch, creditor = new_autoplay_call(
                scenario, scenario_id, settings=self.settings, audit=self.audit,
                call_id=self.call_id,
            )  # fmt: skip
        except (ValueError, FileNotFoundError, KeyError) as e:
            await self._send_one({"type": "error", "message": str(e)})
            return
        self.orch = orch
        self.audit_after = 0
        pause_ms = clamp_pause_ms(data.get("autoplay_pause_ms"))
        self.audit.append(
            self.call_id,
            "orchestrator",
            "call_started",
            {
                "scenario_id": scenario_id,
                "rebased_as_of": rebased_as_of,
                "autoplay": True,
                "autoplay_pause_ms": pause_ms,
            },
        )

        async def on_agent(utt: Utterance) -> None:
            await self._emit(utt)

        async def on_creditor(text: str) -> None:
            async with self._send_lock:
                await _send_creditor_transcript(self.ws, text)

        async def drive() -> None:
            result = await run_autoplay(
                orch,
                creditor,
                on_agent=on_agent,
                on_creditor=on_creditor,
                pause_s=pause_ms / 1000.0,
            )
            self.audit.append(
                self.call_id,
                "orchestrator",
                "autoplay_done",
                {"outcome": result.outcome, "phase": result.phase.value},
            )
            async with self._send_lock:
                await self._audit_tail_and_done()
                await _send(
                    self.ws,
                    {
                        "type": "autoplay_done",
                        "outcome": result.outcome,
                        "phase": result.phase.value,
                        "final_intent": result.final_intent.value,
                        "turns": result.turns,
                    },
                )

        self._autoplay = self._spawn(drive())

    async def _on_wav(self, wav: bytes) -> None:
        assert self.orch is not None
        try:
            with llm_call_scope(self.call_id), queue_wait_scope() as stt_queue:
                text, stt_ms = await transcribe(self.llm, wav)
        except LLMUnavailable as e:
            self.audit.append(self.call_id, "stt", "stt_error", {"message": str(e)})
            async with self._send_lock:
                await _send(self.ws, {"type": "stt_error", "message": str(e)})
                # Client set waiting=true on WAV send — unblock the UI.
                await self._audit_tail_and_done()
            return
        if not text.strip():
            await self._send_one({"type": "turn_done"})
            return
        # Show the rep line immediately; agent reply follows after NLU/NLG.
        async with self._send_lock:
            await _send_creditor_transcript(self.ws, text)
        try:
            utt = await self.orch.on_creditor_text(text)
        except LLMUnavailable as e:
            await self._nlu_unavailable(e)
            return
        await self._emit(utt, stt_ms=stt_ms, stt_queue_ms=stt_queue.ms)

    async def _on_end(self, data: dict[str, Any]) -> None:
        assert self.orch is not None
        await self._emit(await self.orch.on_rep_end())

    async def _on_text(self, data: dict[str, Any]) -> None:
        assert self.orch is not None
        text = str(data.get("text") or "").strip()
        if not text:
            return
        source = data.get("source")
        if source:
            self.audit.append(
                self.call_id,
                "creditor",
                "utterance_source",
                {"source": source, "text": text[:200]},
            )
        oracle = None
        if self.settings.nlu_mode == "oracle" and "oracle" in data:
            oracle = _parse_oracle(data.get("oracle"))
        # Echo before NLU so the chat shows the line while the agent thinks.
        async with self._send_lock:
            await _send_creditor_transcript(self.ws, text)
        try:
            utt = await self.orch.on_creditor_text(text, oracle=oracle)
        except LLMUnavailable as e:
            await self._nlu_unavailable(e)
            return
        await self._emit(utt)

    async def _on_sentence_done(self, data: dict[str, Any]) -> None:
        assert self.orch is not None
        sid = data.get("id")
        if sid is None or str(sid).strip() == "":
            await self._send_one({"type": "error", "message": "sentence_done requires id"})
            return
        orch = self.orch
        unavailable: LLMUnavailable | None = None
        try:
            agreement = await orch.on_sentence_done([str(sid)])
        except LLMUnavailable as e:
            # The ack committed; only the post-NLU drain of queued text failed.
            agreement, unavailable = None, e
            self.audit.append(self.call_id, "nlu", "llm_unavailable", {"message": str(e)})
        async with self._send_lock:
            await _send(self.ws, _phase_payload(orch.session))
            if orch.session.last_eval is not None:
                ev = _eval_payload(orch.session, orch.session.last_eval)
                if ev is not None:
                    await _send(self.ws, ev)
            if agreement is not None:
                await _send(self.ws, _agreement_payload(agreement))
            if unavailable is not None:
                await _send(
                    self.ws, {"type": "error", "message": f"NLU unavailable: {unavailable}"}
                )
            await self._audit_tail_and_done()
        await self._emit_drained()

    async def _on_barge_in(self, data: dict[str, Any]) -> None:
        assert self.orch is not None
        spoken = [str(x) for x in (data.get("spoken_ids") or [])]
        await self.orch.on_barge_in(spoken)
        async with self._send_lock:
            await _send(self.ws, _phase_payload(self.orch.session))
            await self._audit_tail_and_done()

    async def _on_timing(self, data: dict[str, Any]) -> None:
        vad_ms = data.get("vad_end_to_first_audio_ms")
        payload = {"turn": data.get("turn"), "vad_end_to_first_audio_ms": vad_ms}
        self.audit.append(self.call_id, "client", "timing", payload)
        if isinstance(vad_ms, (int, float)):
            LATENCY_BUFFER.record({"vad_end_to_first_audio_ms": float(vad_ms)})
        async with self._send_lock:
            await self._audit_tail_and_done()


@router.websocket("/ws/call/{call_id}")
async def call_socket(websocket: WebSocket, call_id: str, view: str = "operator") -> None:
    """Drive one settlement call; ``view`` (``rep`` | ``operator``) scopes what is sent."""
    await websocket.accept()
    if view not in VIEWS:
        await _send(websocket, {"type": "error", "message": f"unknown view: {view!r}"})
        await websocket.close()
        return
    if _audit is None or _llm is None:
        await _send(
            websocket,
            {"type": "error", "message": "server not configured"},
        )
        await websocket.close()
        return

    conn = _CallConnection(
        _ViewSocket(websocket, view, call_id),
        call_id,
        audit=_audit,
        llm=_llm,
        settings=_settings or get_settings(),
    )
    try:
        await conn.run()
    except WebSocketDisconnect:
        if conn.orch is not None:
            _audit.append(call_id, "orchestrator", "call_ended", {"by": "disconnect"})
        return
    except Exception:
        if conn.orch is not None:
            _audit.append(call_id, "orchestrator", "call_ended", {"by": "error"})
        raise
