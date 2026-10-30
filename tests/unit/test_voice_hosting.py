"""Phase 66 hosting: Cloudflare ICE servers, STUN fallback, and the voice caps.

Pins the ``GET /voice/ice`` shape, that the long-term TURN token never reaches
a response or the log, the STUN-only fallback without keys, the daily minutes
cap and the per-call voice time cap. No network: Cloudflare is an
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog
from app.voice.pipeline import ice
from app.voice.pipeline.budget import CALL_CAP_MESSAGE, VoiceBudget
from app.voice.pipeline.session import CALL_SECONDS, VoiceSession
from tests.unit.test_voice_pipeline import FakeCall, _recv_until

TOKEN = "long-term-secret-token"
CF_REPLY = {
    "iceServers": [
        {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
        {
            "urls": [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turn:turn.cloudflare.com:53?transport=udp",
                "turns:turn.cloudflare.com:443?transport=tcp",
            ],
            "username": "short-user",
            "credential": "short-cred",
        },
    ]
}


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "nlu_mode": "oracle", "nlg_mode": "template", "llm_profile": "offline",
        "db_path": ":memory:", "api_key_pool": {}, "deepgram_api_key": "test-key",
    }  # fmt: skip
    return Settings(**(base | kw))


def _cloudflare(seen: list[httpx.Request], status: int = 201) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json=CF_REPLY if status < 400 else {"errors": []})

    return httpx.MockTransport(handle)


async def test_ice_servers_fetches_short_lived_turn_and_drops_port_53() -> None:
    seen: list[httpx.Request] = []
    s = _settings(cloudflare_turn_key_id="kid", cloudflare_turn_api_token=TOKEN)
    async with httpx.AsyncClient(transport=_cloudflare(seen)) as client:
        servers = await ice.ice_servers(s, client=client)
    assert str(seen[0].url) == ice.GENERATE_URL.format(key_id="kid")
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(seen[0].content) == {"ttl": 3600}
    assert servers == [
        {"urls": ["stun:stun.cloudflare.com:3478"]},
        {
            "urls": [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turns:turn.cloudflare.com:443?transport=tcp",
            ],
            "username": "short-user",
            "credential": "short-cred",
        },
    ]
    assert ice.to_pipecat(servers)[1].username == "short-user"


async def test_server_side_skips_turn_when_voice_server_turn_is_off() -> None:
    seen: list[httpx.Request] = []
    s = _settings(
        cloudflare_turn_key_id="kid", cloudflare_turn_api_token=TOKEN, voice_server_turn=False
    )
    async with httpx.AsyncClient(transport=_cloudflare(seen)) as client:
        assert await ice.server_ice_servers(s, client=client) == ice.STUN_ONLY
        assert seen == []
        # The browser's list still carries TURN.
        assert len(await ice.ice_servers(s, client=client)) == 2


async def test_ice_servers_fall_back_to_stun_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ice, "_warned", False)
    caplog.set_level(logging.WARNING, logger=ice.__name__)
    assert await ice.ice_servers(_settings()) == ice.STUN_ONLY
    assert await ice.ice_servers(_settings()) == ice.STUN_ONLY
    assert len(caplog.records) == 1

    # Cloudflare refuses: STUN only, and the log names the status, never the token.
    caplog.clear()
    s = _settings(cloudflare_turn_key_id="kid", cloudflare_turn_api_token=TOKEN)
    async with httpx.AsyncClient(transport=_cloudflare([], status=401)) as client:
        assert await ice.ice_servers(s, client=client) == ice.STUN_ONLY
    assert "401" in caplog.text and TOKEN not in caplog.text


def test_ice_endpoint_needs_a_started_call_and_never_returns_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        ice.httpx, "AsyncClient", lambda **kw: real(transport=_cloudflare([]), **kw)
    )
    caplog.set_level(logging.DEBUG)
    s = _settings(cloudflare_turn_key_id="kid", cloudflare_turn_api_token=TOKEN)
    audit = AuditLog(tmp_path / "ws.db")
    app = create_app(settings=s, llm=FakeLLM(), audit=audit)
    with TestClient(app) as client:
        assert client.get("/voice/ice?call_id=nope").status_code == 404
        with client.websocket_connect("/ws/call/ice1") as ws:
            ws.send_json({"type": "start", "scenario": "fixtures/demo"})
            _recv_until(ws, "turn_done")
            r = client.get("/voice/ice?call_id=ice1")
    audit.close()
    assert r.status_code == 200
    assert [x["urls"][0] for x in r.json()["iceServers"]] == [
        "stun:stun.cloudflare.com:3478",
        "turn:turn.cloudflare.com:3478?transport=udp",
    ]
    assert TOKEN not in r.text and TOKEN not in caplog.text


# ---------------------------------------------------------------- caps


def test_daily_minutes_cap_refuses_new_sessions(tmp_path: Path) -> None:
    budget = VoiceBudget(tmp_path / "b.db", limit_micros=10**9, limit_seconds=Decimal("120"))
    assert budget.admit() and budget.remaining_seconds() == 120
    budget.add(1, stt_seconds=Decimal("90"), tts_chars=0)
    assert budget.admit() and budget.remaining_seconds() == 30
    budget.add(1, stt_seconds=Decimal("30"), tts_chars=0)
    assert not budget.admit() and budget.remaining_seconds() == 0


class CapCall(FakeCall):
    def __init__(self) -> None:
        super().__init__(None)
        self.errors: list[str] = []

    async def voice_error(self, message: str) -> None:
        self.errors.append(message)


async def test_session_ends_with_a_message_at_the_per_call_cap(tmp_path: Path) -> None:
    CALL_SECONDS["cap1"] = 9.9  # earlier sessions of this call (a reconnect)
    call = CapCall()
    budget = VoiceBudget(tmp_path / "b.db", limit_micros=10**9, limit_seconds=Decimal("600"))
    session = VoiceSession("cap1", call, settings=_settings(voice_session_max_s=10), budget=budget)
    stopped: list[bool] = []

    async def stop() -> None:
        stopped.append(True)

    session.stop = stop  # type: ignore[method-assign]
    assert session.cap_seconds()[1] == CALL_CAP_MESSAGE
    await asyncio.wait_for(session._cap(), 2)
    assert call.errors == [CALL_CAP_MESSAGE]
    assert call.states[-1] == ("ended", "") and stopped == [True]
    assert ("voice_capped", {"seconds": pytest.approx(0.1), "reason": CALL_CAP_MESSAGE}) in [
        (t, p) for t, p in call.rows
    ]
    del CALL_SECONDS["cap1"]


def test_offer_refused_once_the_call_used_its_voice_time(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "ws.db")
    app = create_app(settings=_settings(voice_session_max_s=10), llm=FakeLLM(), audit=audit)
    with TestClient(app) as client, client.websocket_connect("/ws/call/cap2") as ws:
        ws.send_json({"type": "start", "scenario": "fixtures/demo"})
        _recv_until(ws, "turn_done")
        CALL_SECONDS["cap2"] = 10.0
        r = client.post("/voice/offer?call_id=cap2", json={"sdp": "", "type": "offer"})
        assert r.status_code == 429 and r.json()["error"] == CALL_CAP_MESSAGE
        assert _recv_until(ws, "error")[-1]["message"] == CALL_CAP_MESSAGE
    audit.close()
    del CALL_SECONDS["cap2"]
