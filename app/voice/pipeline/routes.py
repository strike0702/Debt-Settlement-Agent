"""HTTP routes of the Pipecat voice path, mounted only when ``VOICE_STACK=pipecat``.

``POST /voice/offer?call_id=…`` takes the browser's SDP offer and returns the
answer; ``PATCH /voice/offer?call_id=…`` adds trickled ICE candidates. The call
must already have started on ``/ws/call/{call_id}``; a second session for the
same call, a missing Deepgram key or a used-up daily voice budget is refused
(409 / 503 / 429) with an ``error`` the UI can show, also sent on the call's
socket. ``GET /voice/events?call_id=…`` is the session timeline for the voice
harness; ``GET /voice/test`` is a minimal page the harness drives. The
Deepgram key stays on the server: the page never sees it, and no key is ever
put in a URL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from app.config import Settings
from app.llm.budget import usd_to_micros
from app.voice import ws as call_ws
from app.voice.pipeline.budget import VoiceBudget
from app.voice.pipeline.session import LAST_EVENTS, SESSIONS, VoiceSession

TEST_PAGE = Path(__file__).with_name("test_page.html")
BUDGET_MESSAGE = "Voice is paused: today's voice budget is used up. Text chat still works."

router = APIRouter()
_handler: SmallWebRTCRequestHandler | None = None
_budget: VoiceBudget | None = None
_settings: Settings | None = None
_runs: set[asyncio.Task[None]] = set()


def configure(settings: Settings, budget: VoiceBudget | None = None) -> None:
    """Bind settings and the daily voice budget (``app.main`` lifespan)."""
    global _handler, _budget, _settings
    _settings = settings
    _budget = budget or VoiceBudget(
        settings.db_path, limit_micros=usd_to_micros(settings.voice_daily_budget_usd)
    )
    _handler = SmallWebRTCRequestHandler()


async def shutdown() -> None:
    """Stop live sessions and close the WebRTC handler (lifespan exit)."""
    for session in list(SESSIONS.values()):
        await session.stop()
    await asyncio.gather(*_runs, return_exceptions=True)
    if _handler is not None:
        await _handler.close()
    if _budget is not None:
        _budget.close()


async def _refuse(call_id: str, status: int, message: str) -> JSONResponse:
    conn = call_ws.live_call(call_id)
    if conn is not None:
        await conn.voice_error(message)
    return JSONResponse({"error": message}, status_code=status)


@router.post("/voice/offer")
async def offer(call_id: str, request: Request) -> Any:
    """SDP offer → answer; starts the call's voice session when WebRTC connects."""
    assert _handler is not None and _budget is not None and _settings is not None
    body = await request.json()
    existing = SESSIONS.get(call_id)
    if existing is not None and body.get("pc_id") is None:
        return await _refuse(call_id, 409, "This call already has a voice session.")
    conn = call_ws.live_call(call_id)
    if conn is None or conn.orch is None:
        return JSONResponse({"error": "Start the call before turning voice on."}, 404)
    if existing is None:
        if _settings.deepgram_api_key is None:
            return await _refuse(call_id, 503, "Voice is not configured on this server.")
        if not _budget.admit():
            conn.audit("voice_refused", {"reason": "budget"})
            return await _refuse(call_id, 429, BUDGET_MESSAGE)
        session = VoiceSession(call_id, conn, settings=_settings, budget=_budget)
        SESSIONS[call_id] = session
        conn.attach_voice(session)
    else:
        session = existing

    async def on_connection(connection: Any) -> None:
        if session.task is not None:
            return  # renegotiation of a running session
        run = asyncio.get_running_loop().create_task(_run(session, connection))
        _runs.add(run)
        run.add_done_callback(_runs.discard)

    return await _handler.handle_web_request(SmallWebRTCRequest.from_dict(body), on_connection)


async def _run(session: VoiceSession, connection: Any) -> None:
    try:
        await session.run(connection)
    finally:
        conn = call_ws.live_call(session.call_id)
        if conn is not None:
            conn.detach_voice(session)


@router.patch("/voice/offer")
async def ice(call_id: str, request: Request) -> dict[str, str]:
    """Trickled ICE candidates for the call's peer connection."""
    assert _handler is not None
    body = await request.json()
    candidates = [
        IceCandidate(
            candidate=c["candidate"], sdp_mid=c["sdp_mid"], sdp_mline_index=c["sdp_mline_index"]
        )
        for c in body.get("candidates", [])
    ]
    await _handler.handle_patch_request(
        SmallWebRTCPatchRequest(pc_id=body["pc_id"], candidates=candidates)
    )
    return {"status": "ok"}


@router.get("/voice/events")
async def events(call_id: str) -> list[dict[str, Any]]:
    """The call's voice timeline (harness); empty when no session ran."""
    session = SESSIONS.get(call_id)
    return session.events if session is not None else LAST_EVENTS.get(call_id, [])


@router.get("/voice/test")
async def test_page() -> FileResponse:
    """Minimal voice page for the harness (not the product UI; Phase 64 builds that)."""
    return FileResponse(TEST_PAGE)
