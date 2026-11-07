"""HTTP routes of the voice path (Pipecat over WebRTC), mounted by ``app.main``.

``POST /voice/offer?call_id=…`` takes the browser's SDP offer and returns the
answer; ``PATCH /voice/offer?call_id=…`` adds trickled ICE candidates. The call
must already have started on ``/ws/call/{call_id}``; a second session for the
same call, a missing Deepgram key or a used-up daily voice budget is refused
(409 / 503 / 429) with an ``error`` the UI can show, also sent on the call's
socket. ``GET /voice/ice?call_id=…`` gives the browser fresh ICE servers
(Cloudflare STUN + short-lived TURN, ``ice``); the Pipecat side fetches its own
before each new offer. Hosted-demo caps: today's Flux minutes
(``voice_daily_minutes``) and voice seconds per call (``voice_session_max_s``)
refuse a new session with a message (429); text chat is never capped here.
Pre-connect (Phase 72): ``POST /voice/offer?call_id=…&warm=1`` on Start call
answers the browser's mic-less offer and only holds the connection
(``warm``): no pipeline, no Deepgram socket, no budget or cap charged.
``POST /voice/live?call_id=…`` (body ``{pc_id}``) is Turn voice on: the same
admission checks as a new offer, then the session starts on the held link and
the reply waits until Flux and TTS are open. 410 means "no usable
pre-connection" (none, closed, or older than the TURN ttl): the browser falls
back to the full ``/voice/offer``. ``DELETE /voice/warm`` drops it (tab hidden);
``POST /voice/connected`` records the browser's click → live time
(``connect_ms``) in the session timeline.

``GET /voice/events?call_id=…`` is the session timeline for the voice
harness; ``GET /voice/test`` is a minimal page the harness drives. The
Deepgram key and the long-term Cloudflare token stay on the server: the page
never sees them, and no key is ever put in a URL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
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
from app.voice.pipeline import warm
from app.voice.pipeline.budget import BUDGET_MESSAGE, CALL_CAP_MESSAGE, VoiceBudget
from app.voice.pipeline.ice import ice_servers, server_ice_servers, to_pipecat
from app.voice.pipeline.session import CALL_SECONDS, LAST_EVENTS, SESSIONS, VoiceSession

TEST_PAGE = Path(__file__).with_name("test_page.html")

# Turn voice on over a held link: wait at most this long for Flux and TTS to open.
LIVE_WAIT_S = 8.0
NO_PRECONNECT = "No usable voice pre-connection; connect again."

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
        settings.db_path,
        limit_micros=usd_to_micros(settings.voice_daily_budget_usd),
        limit_seconds=settings.voice_daily_minutes * 60,
    )
    _handler = SmallWebRTCRequestHandler()


async def shutdown() -> None:
    """Stop live sessions and close the WebRTC handler (lifespan exit)."""
    await warm.close_all()
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


NOT_STARTED = "Start the call before turning voice on."


async def _admit(call_id: str, conn: Any) -> JSONResponse | None:
    """A new voice session's checks (key, daily budget, per-call cap); None = admitted."""
    assert _budget is not None and _settings is not None
    if _settings.deepgram_api_key is None:
        return await _refuse(call_id, 503, "Voice is not configured on this server.")
    if not _budget.admit():
        conn.audit("voice_refused", {"reason": "budget"})
        return await _refuse(call_id, 429, BUDGET_MESSAGE)
    if CALL_SECONDS.get(call_id, 0.0) >= _settings.voice_session_max_s:
        conn.audit("voice_refused", {"reason": "call_cap"})
        return await _refuse(call_id, 429, CALL_CAP_MESSAGE)
    return None


def _new_session(call_id: str, conn: Any) -> VoiceSession:
    assert _budget is not None and _settings is not None
    session = VoiceSession(call_id, conn, settings=_settings, budget=_budget)
    SESSIONS[call_id] = session
    conn.attach_voice(session)
    return session


def _start(session: VoiceSession, connection: Any) -> asyncio.Task[None]:
    run = asyncio.get_running_loop().create_task(_run(session, connection))
    _runs.add(run)
    run.add_done_callback(_runs.discard)
    return run


@router.post("/voice/offer")
async def offer(call_id: str, request: Request, warm_up: bool = Query(False, alias="warm")) -> Any:
    """SDP offer → answer; starts the call's voice session when WebRTC connects.

    With ``warm=1`` (Start call) the connection is only held for a later
    ``/voice/live``; refusals are not sent on the call's socket (the user did
    not ask for voice yet; Turn voice on reports them).
    """
    assert _handler is not None and _budget is not None and _settings is not None
    body = await request.json()
    existing = SESSIONS.get(call_id)
    if existing is not None and body.get("pc_id") is None:
        if warm_up:
            return JSONResponse({"error": "This call already has a voice session."}, 409)
        return await _refuse(call_id, 409, "This call already has a voice session.")
    conn = call_ws.live_call(call_id)
    if conn is None or conn.orch is None:
        return JSONResponse({"error": NOT_STARTED}, 404)
    request_obj = SmallWebRTCRequest.from_dict(body)
    if request_obj.pc_id is None:
        # A new peer connection: aiortc gets its own fresh TURN credentials
        # (STUN only when VOICE_SERVER_TURN is off).
        _handler.update_ice_servers(to_pipecat(await server_ice_servers(_settings)))
    if warm_up:
        if _settings.deepgram_api_key is None or request_obj.pc_id is not None:
            return JSONResponse({"error": "Voice is not configured on this server."}, 503)
        held: list[Any] = []

        async def keep(connection: Any) -> None:
            held.append(connection)

        answer = await _handler.handle_web_request(request_obj, keep)
        if held:
            await warm.hold(
                call_id,
                held[0],
                idle_s=_settings.voice_preconnect_idle_s,
                call_live=lambda: call_ws.live_call(call_id) is not None,
            )
        return answer
    if existing is None:
        refusal = await _admit(call_id, conn)
        if refusal is not None:
            return refusal
        await warm.close(call_id)  # the full path replaces any held link
        session = _new_session(call_id, conn)
    else:
        session = existing

    async def on_connection(connection: Any) -> None:
        if session.task is not None:
            return  # renegotiation of a running session
        _start(session, connection)

    return await _handler.handle_web_request(request_obj, on_connection)


@router.post("/voice/live")
async def live(call_id: str, request: Request) -> Any:
    """Turn voice on over the held pre-connection: start the session, answer once it is live.

    410 = no usable pre-connection (the browser does the full offer instead).
    """
    assert _settings is not None
    body = await request.json()
    conn = call_ws.live_call(call_id)
    if conn is None or conn.orch is None:
        return JSONResponse({"error": NOT_STARTED}, 404)
    if SESSIONS.get(call_id) is not None:
        return await _refuse(call_id, 409, "This call already has a voice session.")
    connection = await warm.take(call_id, body.get("pc_id"), ttl_s=_settings.voice_turn_ttl_s)
    if connection is None:
        return JSONResponse({"error": NO_PRECONNECT}, 410)
    refusal = await _admit(call_id, conn)
    if refusal is not None:
        await warm.discard(connection)
        return refusal
    session = _new_session(call_id, conn)
    run = _start(session, connection)
    ready = asyncio.ensure_future(session.live.wait())
    await asyncio.wait({ready, run}, timeout=LIVE_WAIT_S, return_when=asyncio.FIRST_COMPLETED)
    if not session.live.is_set():
        ready.cancel()
        await session.stop()
        return JSONResponse({"error": "voice connection failed"}, 502)
    return {"status": "live"}


@router.delete("/voice/warm")
async def drop_warm(call_id: str, pc_id: str | None = None) -> dict[str, str]:
    """The browser dropped its idle pre-connection (tab hidden, new call)."""
    await warm.close(call_id, pc_id)
    return {"status": "ok"}


@router.post("/voice/connected")
async def connected(call_id: str, request: Request) -> dict[str, str]:
    """The browser's click → live time, logged as ``connect`` in the session timeline."""
    body = await request.json()
    session = SESSIONS.get(call_id)
    ms, path = body.get("connect_ms"), body.get("path")
    if session is not None and isinstance(ms, int | float) and path in ("warm", "full"):
        session.log("connect", connect_ms=round(ms), path=path)
    return {"status": "ok"}


async def _run(session: VoiceSession, connection: Any) -> None:
    try:
        await session.run(connection)
    finally:
        conn = call_ws.live_call(session.call_id)
        if conn is not None:
            conn.detach_voice(session)


@router.get("/voice/ice")
async def ice_config(call_id: str) -> Any:
    """Fresh ICE servers for the browser's peer connection (only for a started call)."""
    assert _settings is not None
    conn = call_ws.live_call(call_id)
    if conn is None or conn.orch is None:
        return JSONResponse({"error": NOT_STARTED}, 404)
    return {"iceServers": await ice_servers(_settings)}


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
