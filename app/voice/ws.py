"""WebSocket ``/ws/call/{call_id}`` protocol for the voice UI.

Client events: ``start``, ``end``, binary WAV, ``text``, ``sentence_done``,
``barge_in``, ``timing``. Server events: ``transcript``, ``say``, ``belief``,
``eval``, ``blocked``, ``escalate``, ``latency``, ``audit``, ``phase``,
``stt_error``, ``turn_done``. Speaks through ``Orchestrator``; STT via
``app.voice.stt``. Does not own policy or NLG.
"""

from __future__ import annotations

import json
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
    load_scenario,
    resolve_scenario_dir,
    scenario_from_payload,
)
from app.llm.client import LLMUnavailable
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
    return {
        "type": "eval",
        "feasible": summary.feasible,
        "shape": summary.shape,
        "offer_total_cents": summary.offer_total_cents,
        "program_fee_cents": summary.program_fee_cents,
        "assumed_fields": list(summary.assumed_fields),
        "agreed_bp": session.agreed_bp
        if session.agreed_bp is not None
        else (
            session.pending.pending_agreed_bp if session.pending is not None else None
        ),
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


async def _emit_utterance(
    ws: WebSocket,
    orch: Orchestrator,
    utt: Utterance,
    *,
    creditor_text: str | None,
    stt_ms: float | None,
    audit_after: int,
) -> int:
    session = orch.session
    blocked = bool(session.last_blocked)

    if creditor_text is not None:
        await _send(
            ws,
            {
                "type": "transcript",
                "role": "creditor",
                "text": creditor_text,
                "spoken": True,
                "sentence_id": None,
                "blocked": False,
            },
        )

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
    latency = {
        "type": "latency",
        "turn": session.neg.turn_idx,
        "stt_ms": timings.get("stt_ms"),
        "nlu_ms": timings.get("nlu_ms"),
        "policy_ms": timings.get("policy_ms"),
        "nlg_ms": timings.get("nlg_ms"),
        "server_total_ms": timings.get("server_total_ms"),
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

    settings = _settings or get_settings()
    orch: Orchestrator | None = None
    audit_after = 0
    started = False

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if "bytes" in message and message["bytes"] is not None:
                if orch is None or not started:
                    await _send(
                        websocket,
                        {"type": "error", "message": "call not started"},
                    )
                    continue
                wav = message["bytes"]
                try:
                    text, stt_ms = await transcribe(_llm, wav)
                except LLMUnavailable as e:
                    _audit.append(
                        call_id,
                        "stt",
                        "stt_error",
                        {"message": str(e)},
                    )
                    await _send(
                        websocket,
                        {"type": "stt_error", "message": str(e)},
                    )
                    audit_after = await _send_audit_tail(
                        websocket, _audit, call_id, after_id=audit_after
                    )
                    continue
                if not text.strip():
                    continue
                utt = await orch.on_creditor_text(text)
                audit_after = await _emit_utterance(
                    websocket,
                    orch,
                    utt,
                    creditor_text=text,
                    stt_ms=stt_ms,
                    audit_after=audit_after,
                )
                continue

            raw_text = message.get("text")
            if raw_text is None:
                continue
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                await _send(websocket, {"type": "error", "message": "invalid json"})
                continue

            event = data.get("type")
            if event == "start":
                scenario_id = data.get("scenario_id")
                rebased_as_of = None
                try:
                    from datetime import date as _date

                    custom = data.get("scenario_payload")
                    if isinstance(custom, dict):
                        scenario = scenario_from_payload(
                            custom,
                            scenario_id=str(scenario_id or "custom"),
                            rebase_to=_date.today(),
                        )
                        scenario_id = scenario.id
                        rebased_as_of = scenario.client.as_of_date.isoformat()
                    elif scenario_id:
                        path = resolve_scenario_dir(str(scenario_id))
                        scenario = load_scenario(path, rebase_to=_date.today())
                        rebased_as_of = scenario.client.as_of_date.isoformat()
                    else:
                        # Legacy path for unit tests (fixtures/demo, no rebase).
                        scenario_path = data.get("scenario") or "fixtures/demo"
                        path = Path(scenario_path)
                        if not path.is_dir():
                            await _send(
                                websocket,
                                {
                                    "type": "error",
                                    "message": f"scenario not found: {path}",
                                },
                            )
                            continue
                        # Only allow under fixtures/
                        resolved = path.resolve()
                        fixtures_root = Path("fixtures").resolve()
                        if fixtures_root not in resolved.parents and resolved != fixtures_root:
                            await _send(
                                websocket,
                                {"type": "error", "message": "scenario path denied"},
                            )
                            continue
                        scenario = load_scenario(path)
                        scenario_id = scenario.id
                except (ValueError, FileNotFoundError, TypeError, KeyError) as e:
                    await _send(
                        websocket,
                        {"type": "error", "message": str(e)},
                    )
                    continue
                session = CallSession(scenario=scenario, call_id=call_id)
                orch = Orchestrator(
                    session,
                    llm=_llm,
                    settings=settings,
                    audit=_audit,
                    auto_ack=False,
                )
                _audit.append(
                    call_id,
                    "orchestrator",
                    "call_started",
                    {
                        "scenario_id": scenario_id,
                        "rebased_as_of": rebased_as_of,
                    },
                )
                utt = await orch.start()
                started = True
                audit_after = await _emit_utterance(
                    websocket,
                    orch,
                    utt,
                    creditor_text=None,
                    stt_ms=None,
                    audit_after=0,
                )
                continue

            if orch is None or not started:
                await _send(
                    websocket,
                    {"type": "error", "message": "call not started"},
                )
                continue

            if event == "end":
                utt = await orch.on_rep_end()
                audit_after = await _emit_utterance(
                    websocket,
                    orch,
                    utt,
                    creditor_text=None,
                    stt_ms=None,
                    audit_after=audit_after,
                )
                continue

            if event == "text":
                text = str(data.get("text") or "").strip()
                if not text:
                    continue
                source = data.get("source")
                if source:
                    _audit.append(
                        orch.session.call_id,
                        "creditor",
                        "utterance_source",
                        {"source": source, "text": text[:200]},
                    )
                oracle = None
                if settings.nlu_mode == "oracle" and "oracle" in data:
                    oracle = _parse_oracle(data.get("oracle"))
                utt = await orch.on_creditor_text(text, oracle=oracle)
                audit_after = await _emit_utterance(
                    websocket,
                    orch,
                    utt,
                    creditor_text=text,
                    stt_ms=None,
                    audit_after=audit_after,
                )
            elif event == "sentence_done":
                sid = data.get("id")
                ids = [str(sid)] if sid is not None else []
                agreement = await orch.on_sentence_done(ids)
                await _send(websocket, _phase_payload(orch.session))
                if orch.session.last_eval is not None:
                    ev = _eval_payload(orch.session, orch.session.last_eval)
                    if ev is not None:
                        await _send(websocket, ev)
                if agreement is not None:
                    await _send(
                        websocket,
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
                audit_after = await _send_audit_tail(
                    websocket, _audit, orch.session.call_id, after_id=audit_after
                )
                await _send(websocket, {"type": "turn_done"})
            elif event == "barge_in":
                spoken = [str(x) for x in (data.get("spoken_ids") or [])]
                await orch.on_barge_in(spoken)
                await _send(websocket, _phase_payload(orch.session))
                audit_after = await _send_audit_tail(
                    websocket, _audit, orch.session.call_id, after_id=audit_after
                )
                await _send(websocket, {"type": "turn_done"})
            elif event == "timing":
                turn = data.get("turn")
                vad_ms = data.get("vad_end_to_first_audio_ms")
                payload = {
                    "turn": turn,
                    "vad_end_to_first_audio_ms": vad_ms,
                }
                _audit.append(
                    orch.session.call_id, "client", "timing", payload
                )
                if isinstance(vad_ms, (int, float)):
                    LATENCY_BUFFER.record(
                        {"vad_end_to_first_audio_ms": float(vad_ms)}
                    )
                audit_after = await _send_audit_tail(
                    websocket, _audit, orch.session.call_id, after_id=audit_after
                )
                await _send(websocket, {"type": "turn_done"})
            else:
                await _send(
                    websocket,
                    {"type": "error", "message": f"unknown event: {event}"},
                )
    except WebSocketDisconnect:
        if orch is not None:
            _audit.append(
                call_id,
                "orchestrator",
                "call_ended",
                {"by": "disconnect"},
            )
        return
    except Exception:
        if orch is not None:
            _audit.append(
                call_id,
                "orchestrator",
                "call_ended",
                {"by": "error"},
            )
        raise
