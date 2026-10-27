"""STT hallucination screen: Whisper text on noise or TTS echo is dropped, real answers pass.

Covers the text filter (bias-prompt repeats, filler, known silence phrases),
the ``verbose_json`` segment check, the client asking Whisper for segment
scores (fake HTTP), and the call socket treating a dropped clip like silence
with an ``stt_dropped`` audit row. All hermetic: FakeLLM and MockTransport.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.client import FakeLLM, LLMClient, SttSegment, Transcription
from app.main import create_app
from app.store.audit import AuditLog
from app.voice.stt import (
    drop_reason,
    segments_drop_reason,
    text_drop_reason,
    transcribe_screened,
)

_SPEECH = SttSegment(no_speech_prob=0.02, avg_logprob=-0.25, compression_ratio=1.1)
_SILENCE = SttSegment(no_speech_prob=0.85, avg_logprob=-1.2, compression_ratio=1.3)
_GARBLED = SttSegment(no_speech_prob=0.1, avg_logprob=-1.8, compression_ratio=1.0)
_LOOP = SttSegment(no_speech_prob=0.1, avg_logprob=-0.4, compression_ratio=3.1)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("and payment, payment, payment, and payment.", "prompt_echo"),
        ("Payment, payment.", "prompt_echo"),
        ("Monthly payments, monthly payments.", "prompt_echo"),
        ("Thank you.", "known_hallucination"),
        ("you", "known_hallucination"),
        ("Thanks for watching!", "known_hallucination"),
        ("Um.", "filler"),
        ("Uh, and, um...", "filler"),
    ],
)
def test_text_filter_drops_hallucinations(text: str, reason: str) -> None:
    assert text_drop_reason(text) == reason


@pytest.mark.parametrize(
    "text",
    [
        "It's three",
        "$35",
        "Correct.",
        "No",
        "Yes.",
        "Okay.",
        "Balloon.",
        "Monthly payments.",
        "Minimum payment is fifty dollars.",
        "Three payments, three payments max.",
        "Thank you, that works.",
        "45 percent",
        "",
    ],
)
def test_text_filter_keeps_real_answers(text: str) -> None:
    assert text_drop_reason(text) is None


def test_segment_check_needs_every_segment_to_look_like_noise() -> None:
    assert segments_drop_reason(None) is None
    assert segments_drop_reason(()) is None
    assert segments_drop_reason((_SILENCE,)) == "no_speech"
    assert segments_drop_reason((_GARBLED, _LOOP)) == "no_speech"
    assert segments_drop_reason((_SILENCE, _SPEECH)) is None
    # High no_speech_prob alone (a confident decode) is kept, as Whisper does.
    assert segments_drop_reason((SttSegment(0.9, -0.3),)) is None


def test_drop_reason_checks_segments_then_text() -> None:
    assert drop_reason(Transcription("It's three", (_SILENCE,))) == "no_speech"
    assert drop_reason(Transcription("It's three", (_SPEECH,))) is None
    assert drop_reason(Transcription("payment, payment", (_SPEECH,))) == "prompt_echo"


@pytest.mark.asyncio
async def test_screened_transcript_blanks_a_dropped_clip() -> None:
    fake = FakeLLM()
    fake.enqueue("stt", "and payment, payment, payment, and payment.")
    fake.enqueue("stt", Transcription("Fifty.", (_SILENCE,)))
    fake.enqueue("stt", "$35")
    first = await transcribe_screened(fake, b"RIFF")
    assert (first.text, first.dropped) == ("", "prompt_echo")
    assert first.raw_text.startswith("and payment")
    assert (await transcribe_screened(fake, b"RIFF")).dropped == "no_speech"
    kept = await transcribe_screened(fake, b"RIFF")
    assert (kept.text, kept.dropped) == ("$35", None)


# ---------------------------------------------------------------- client


def _yaml(tmp: Path, model: str) -> Path:
    p = tmp / "providers.yaml"
    p.write_text(
        f"""
profiles:
  demo:
    stt: [primary/{model}]
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
"""
    )
    return p


def _settings() -> Settings:
    return Settings(
        groq_api_key="pk",
        llm_profile="demo",
        llm_cache=False,
        llm_cache_path="unused.db",
        api_key_pool={},
    )


async def _transcribe_with(
    tmp: Path, model: str, body: dict[str, Any]
) -> tuple[Transcription, bytes]:
    seen: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read())
        return httpx.Response(200, json=body)

    client = LLMClient(
        _settings(),
        providers_path=_yaml(tmp, model),
        http_clients={"primary": httpx.AsyncClient(transport=httpx.MockTransport(handler))},
        skip_health_check=True,
    )
    try:
        result = await client.transcribe_detailed(b"RIFF")
    finally:
        await client.aclose()
    return result, seen[0]


@pytest.mark.asyncio
async def test_client_asks_whisper_for_verbose_json_and_reads_scores(tmp_path: Path) -> None:
    seg = {"text": "x", "no_speech_prob": 0.81, "avg_logprob": -1.3, "compression_ratio": 1.2}
    body = {"text": " and payment, payment.", "segments": [seg]}
    result, sent = await _transcribe_with(tmp_path, "whisper-large-v3-turbo", body)
    assert b"verbose_json" in sent
    assert result.segments == (SttSegment(0.81, -1.3, 1.2),)
    assert drop_reason(result) == "no_speech"


@pytest.mark.asyncio
async def test_client_skips_verbose_json_for_non_whisper_models(tmp_path: Path) -> None:
    result, sent = await _transcribe_with(tmp_path, "gpt-4o-transcribe", {"text": "It's three"})
    assert b"verbose_json" not in sent
    assert result == Transcription("It's three", None)


# ---------------------------------------------------------------- socket


def _ws_settings() -> Settings:
    return Settings(
        nlu_mode="oracle",
        nlg_mode="template",
        llm_profile="offline",
        db_path=":memory:",
    )


def _recv_until(ws: Any, predicate: Any, *, limit: int = 80) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []
    for _ in range(limit):
        raw = ws.receive_json()
        got.append(raw)
        if predicate(raw):
            break
    return got


def test_socket_treats_a_dropped_clip_like_silence_and_audits_it(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "ws_audit.db")
    fake = FakeLLM()
    fake.enqueue("stt", "and payment, payment, payment, and payment.")
    fake.enqueue("stt", Transcription("Okay.", (_SILENCE,)))
    app = create_app(settings=_ws_settings(), llm=fake, audit=audit)
    with TestClient(app) as client, client.websocket_connect("/ws/call/stt-drop?view=rep") as ws:
        ws.send_json({"type": "start", "scenario": "fixtures/demo"})
        _recv_until(ws, lambda m: m.get("type") == "turn_done")
        frames: list[dict[str, Any]] = []
        for _ in range(2):
            ws.send_bytes(b"RIFF....")
            frames += _recv_until(ws, lambda m: m.get("type") == "turn_done")
    assert not [f for f in frames if f["type"] in ("transcript", "say", "stt_error", "error")]
    # The rep view never sees the dropped text.
    assert "payment, payment" not in str(frames)
    rows = [e for e in audit.for_call("stt-drop") if e["type"] == "stt_dropped"]
    assert [r["payload"]["reason"] for r in rows] == ["prompt_echo", "no_speech"]
    assert rows[0]["payload"]["text"].startswith("and payment")
    audit.close()
