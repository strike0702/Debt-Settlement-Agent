"""Phase 72 pre-connect: an idle WebRTC link on Start call, live only on Turn voice on.

Pins that a warm offer holds the connection without a session (no pipeline,
no Deepgram, nothing on the budget or the per-call cap), that ``/voice/live``
starts the session on that same connection, that a missing or ttl-expired
pre-connection is refused with 410 (the browser's full-connect fallback), and
that the held link closes after the idle timeout or when its call ends. No
network and no Pipecat pipeline: the WebRTC handler and ``VoiceSession.run``
are fakes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog
from app.voice.pipeline import routes, warm
from app.voice.pipeline.session import CALL_SECONDS, SESSIONS, VoiceSession
from tests.unit.test_voice_hosting import _settings
from tests.unit.test_voice_pipeline import _recv_until


class FakePc:
    connectionState = "connected"


class FakeConn:
    def __init__(self, pc_id: str = "pcW") -> None:
        self.pc_id = pc_id
        self.pc = FakePc()
        self.closed = False

    async def disconnect(self) -> None:
        self.closed = True


class FakeHandler:
    def __init__(self) -> None:
        self.conns: list[FakeConn] = []

    def update_ice_servers(self, _servers: Any) -> None:
        pass

    async def handle_web_request(self, req: Any, cb: Any) -> dict[str, str]:
        conn = FakeConn()
        self.conns.append(conn)
        await cb(conn)
        return {"sdp": "answer", "type": "answer", "pc_id": conn.pc_id}

    async def close(self) -> None:
        pass


def test_warm_offer_holds_the_link_and_live_starts_the_session_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[Any] = []

    async def fake_run(self: VoiceSession, connection: Any) -> None:
        ran.append(connection)
        self.live.set()

    async def no_audio(self: VoiceSession, *_: Any) -> None:
        pass

    monkeypatch.setattr(VoiceSession, "run", fake_run)
    monkeypatch.setattr(VoiceSession, "speak_sentences", no_audio)
    audit = AuditLog(tmp_path / "ws.db")
    app = create_app(settings=_settings(), llm=FakeLLM(), audit=audit)
    with TestClient(app) as client, client.websocket_connect("/ws/call/w1") as ws:
        ws.send_json({"type": "start", "scenario": "fixtures/demo"})
        _recv_until(ws, "turn_done")
        handler = FakeHandler()
        monkeypatch.setattr(routes, "_handler", handler)
        charged: list[Any] = []
        assert routes._budget is not None
        monkeypatch.setattr(routes._budget, "add", lambda *a, **k: charged.append(a))
        assert client.post("/voice/live?call_id=w1", json={"pc_id": "pcW"}).status_code == 410

        r = client.post("/voice/offer?call_id=w1&warm=1", json={"sdp": "o", "type": "offer"})
        assert r.status_code == 200 and r.json()["pc_id"] == "pcW"
        # Idle: no session, no pipeline, nothing spent or counted.
        assert "w1" not in SESSIONS and ran == []
        assert warm.WARM["w1"].connection is handler.conns[0]
        assert charged == [] and "w1" not in CALL_SECONDS

        r = client.post("/voice/live?call_id=w1", json={"pc_id": "pcW"})
        assert r.status_code == 200 and r.json() == {"status": "live"}
        assert ran == [handler.conns[0]] and "w1" not in warm.WARM
        assert not handler.conns[0].closed
    audit.close()
    SESSIONS.pop("w1", None)


async def test_take_refuses_a_link_older_than_the_turn_ttl() -> None:
    now = [0.0]
    conn = FakeConn()
    await warm.hold("t1", conn, idle_s=600, call_live=lambda: True, clock=lambda: now[0])
    assert await warm.take("t1", "other", ttl_s=3600, clock=lambda: now[0]) is None
    now[0] = 3600.0
    assert await warm.take("t1", "pcW", ttl_s=3600, clock=lambda: now[0]) is None
    assert conn.closed and "t1" not in warm.WARM


async def test_idle_link_closes_after_the_timeout_and_when_the_call_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(warm, "CHECK_S", 0.01)
    idle = FakeConn()
    w = await warm.hold("i1", idle, idle_s=0.05, call_live=lambda: True)
    live = [True]
    ended = FakeConn("pcE")
    w2 = await warm.hold("i2", ended, idle_s=600, call_live=lambda: live[0])
    live[0] = False
    await asyncio.wait_for(asyncio.gather(w.reaper, w2.reaper), 2)  # type: ignore[arg-type]
    assert idle.closed and ended.closed
    assert "i1" not in warm.WARM and "i2" not in warm.WARM
