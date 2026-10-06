"""WS event models: JSON Schema export, and every frame of a real call validates."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.events import (
    SERVER_EVENT_ADAPTER,
    SERVER_EVENT_TYPES,
    export_schema,
    main,
    schema_text,
)
from tests.wsutil import make_client, scripted_easy_deal

_COMMITTED = Path("web/src/types/events.schema.json")


def test_committed_schema_matches_models() -> None:
    """CI regenerates too; this catches a stale file before push."""
    assert _COMMITTED.read_text() == schema_text()


def test_cli_writes_schema(tmp_path: Path) -> None:
    out = tmp_path / "events.schema.json"
    assert main(["--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data == export_schema()
    # Both directions of the protocol are exported.
    assert {"ServerEvent", "ClientEvent"} <= set(data["properties"])


def test_schema_lists_every_event_type() -> None:
    expected = {
        "transcript", "say", "belief", "eval", "blocked", "escalate", "latency",
        "audit", "phase", "agreement", "stt_error", "error", "turn_done",
        "turn_trace", "autoplay_done",
    }  # fmt: skip
    assert set(SERVER_EVENT_TYPES) == expected
    text = schema_text()
    for name in expected:
        assert f'"{name}"' in text


def test_every_frame_of_a_call_validates(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/schema-1") as ws:
        frames = scripted_easy_deal(ws)
    types = {f["type"] for f in frames}
    assert {"say", "turn_trace", "agreement", "audit", "eval"} <= types
    for frame in frames:
        SERVER_EVENT_ADAPTER.validate_python(frame)


def test_every_frame_of_an_autoplay_call_validates(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/schema-2") as ws:
        ws.send_json(
            {"type": "start", "scenario_id": "rescue_escalate", "autoplay": True,
             "autoplay_pause_ms": 0}
        )  # fmt: skip
        frames = []
        while True:
            frames.append(ws.receive_json())
            if frames[-1]["type"] == "autoplay_done":
                break
    for frame in frames:
        SERVER_EVENT_ADAPTER.validate_python(frame)


def test_web_fixture_frames_match_the_protocol() -> None:
    """The UI's recorded fixture is valid wire data (``tts_onset`` is client-local)."""
    import json
    from pathlib import Path

    from app.schemas.events import SERVER_EVENT_ADAPTER

    path = Path(__file__).resolve().parents[2] / "web/src/fixtures/call_easy_deal.json"
    frames = json.loads(path.read_text())["frames"]
    server = [f["ev"] for f in frames if f["ev"]["type"] != "tts_onset"]
    assert len(server) > 100 and len(server) < len(frames)
    for ev in server:
        SERVER_EVENT_ADAPTER.validate_python(ev)
