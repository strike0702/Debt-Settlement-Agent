"""WebSocket ``/ws/call/{call_id}`` protocol for the voice UI.

Client events: ``start``, ``end``, binary WAV, ``text``, ``sentence_done``,
``barge_in``, ``timing``. Server events (every ``type`` this module sends):
``transcript``, ``say``, ``belief``, ``eval``, ``blocked``, ``escalate``,
``latency``, ``audit``, ``phase``, ``agreement``, ``stt_error``, ``error``,
``turn_done``. Speaks through ``Orchestrator``; STT via ``app.voice.stt``, run
inside ``llm_call_scope(call_id)`` so the STT call is audited.

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
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.adapter.engine_adapter import EvalSummary
from app.agent.nlu_types import TurnAnalysis
from app.agent.orchestrator import Orchestrator, Utterance
from app.agent.policy import Intent
from app.agent.session import CallSession
from app.config import Settings, get_settings
from app.domain.scenario import (
    CallScenario,
    load_scenario,
    resolve_scenario_dir,
    scenario_from_payload,
)
from app.llm.call_audit import llm_call_scope
from app.llm.client import LLMUnavailable, queue_wait_scope
from app.store.audit import AuditLog
from app.voice.metrics_buf import LATENCY_BUFFER
from app.voice.stt import transcribe

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


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_json(payload)


async def _send_audit_tail(
    ws: WebSocket,
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


async def _send_creditor_transcript(ws: WebSocket, text: str) -> None:
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


async def _emit_utterance(
    ws: WebSocket,
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
        self, ws: WebSocket, call_id: str, *, audit: AuditLog, llm: Any, settings: Settings
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

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

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

    async def _on_start(self, data: dict[str, Any]) -> None:
        try:
            scenario, scenario_id, rebased_as_of = _load_start_scenario(data)
        except _StartError as e:
            await self._send_one({"type": "error", "message": str(e)})
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
        utt = await self.orch.on_creditor_text(text)
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
        utt = await self.orch.on_creditor_text(text, oracle=oracle)
        await self._emit(utt)

    async def _on_sentence_done(self, data: dict[str, Any]) -> None:
        assert self.orch is not None
        sid = data.get("id")
        if sid is None or str(sid).strip() == "":
            await self._send_one({"type": "error", "message": "sentence_done requires id"})
            return
        orch = self.orch
        agreement = await orch.on_sentence_done([str(sid)])
        async with self._send_lock:
            await _send(self.ws, _phase_payload(orch.session))
            if orch.session.last_eval is not None:
                ev = _eval_payload(orch.session, orch.session.last_eval)
                if ev is not None:
                    await _send(self.ws, ev)
            if agreement is not None:
                await _send(
                    self.ws,
                    {
                        "type": "agreement",
                        "creditor": agreement.creditor,
                        "bp": agreement.bp,
                        "offer_total": agreement.offer_total,
                        "status": agreement.status,
                        "assumed_fields": list(agreement.assumed_fields),
                        "rows": agreement.rows,
                    },
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
async def call_socket(websocket: WebSocket, call_id: str) -> None:
    """Drive one settlement call over the PLAN §8 event protocol."""
    await websocket.accept()
    if _audit is None or _llm is None:
        await _send(
            websocket,
            {"type": "error", "message": "server not configured"},
        )
        await websocket.close()
        return

    conn = _CallConnection(
        websocket, call_id, audit=_audit, llm=_llm, settings=_settings or get_settings()
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
