"""Per-call minimum client cushion over the WebSocket (Phase 59 follow-up).

``start.min_cushion_cents`` overrides the firm default for that call only and is
validated; absent means the firm default (``firm.json``). It is PRIVATE: the rep stream
never carries it, NLG never sees it, and only the operator trace says whether the
drafted plan kept it.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app import autoplay as autoplay_mod
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.domain.scenario import (
    MAX_MIN_CUSHION_CENTS,
    SCENARIO_TEMPLATE,
    load_scenario,
    parse_min_cushion,
    scenario_payload_errors,
)
from app.voice import ws as ws_mod
from tests.wsutil import leaked_private_values, make_client

_SESSIONS: list[CallSession] = []
_SPOKEN: list[Any] = []


class _RecordingSession(CallSession):
    def __post_init__(self) -> None:
        super().__post_init__()
        _SESSIONS.append(self)


@pytest.fixture(autouse=True)
def _record(monkeypatch: pytest.MonkeyPatch) -> None:
    _SESSIONS.clear()
    _SPOKEN.clear()
    monkeypatch.setattr(ws_mod, "CallSession", _RecordingSession)
    monkeypatch.setattr(autoplay_mod, "CallSession", _RecordingSession)
    orig = Orchestrator._speak_move

    async def spy(self: Orchestrator, action: Any, **kw: Any) -> list[str]:
        _SPOKEN.append(action)
        return await orig(self, action, **kw)

    monkeypatch.setattr(Orchestrator, "_speak_move", spy)


def _autoplay(ws: Any, scenario_id: str, **extra: Any) -> list[dict]:
    ws.send_json({"type": "start", "scenario_id": scenario_id, "autoplay": True,
                  "autoplay_pause_ms": 0, **extra})
    frames: list[dict] = []
    while not frames or frames[-1]["type"] not in ("autoplay_done", "error"):
        frames.append(ws.receive_json())
    return frames


def _cushions(frames: list[dict]) -> list[dict]:
    return [f["cushion"] for f in frames if f["type"] == "turn_trace" and f.get("cushion")]


@pytest.mark.parametrize(("cents", "kept"), [(10_000, True), (MAX_MIN_CUSHION_CENTS, False)])
def test_start_override_reaches_engine_and_operator_trace(
    tmp_path: Path, cents: int, kept: bool
) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/c-op") as ws:
        frames = _autoplay(ws, "easy_deal", min_cushion_cents=cents)
    session = _SESSIONS[-1]
    assert session.scenario.min_cushion_cents == cents
    assert any(f["type"] == "agreement" for f in frames)
    marks = _cushions(frames)
    assert marks and all(m == {"min_cushion_cents": cents, "kept": kept} for m in marks)
    assert ("money", cents) in session.private_blocklist
    rows = [f for f in frames if f["type"] == "audit" and f["event"] == "min_cushion"]
    assert rows and rows[0]["private"] and rows[0]["payload"]["source"] == "call"
    # NLG never receives the cushion: no fact on any spoken move carries it.
    assert _SPOKEN
    for action in _SPOKEN:
        assert "min_cushion" not in action.facts
        assert all(f.value != cents for f in action.facts.values())


def test_rep_stream_never_carries_the_cushion(tmp_path: Path) -> None:
    cents = 12_345
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/c-rep?view=rep") as ws:
        frames = _autoplay(ws, "easy_deal", min_cushion_cents=cents)
    assert _SESSIONS[-1].scenario.min_cushion_cents == cents
    assert not any(f["type"] == "turn_trace" for f in frames)
    assert "min_cushion" not in json.dumps(frames)
    assert leaked_private_values(frames, {("money", cents)}, ref=date.today()) == []


def test_absent_start_field_uses_the_firm_default(tmp_path: Path) -> None:
    payload = copy.deepcopy(SCENARIO_TEMPLATE)
    payload["firm"]["min_cushion_cents"] = 7_000
    assert scenario_payload_errors(payload) == []
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/c-firm") as ws:
        ws.send_json({"type": "start", "scenario_id": "custom", "scenario_payload": payload})
        ws.receive_json()
    assert _SESSIONS[-1].scenario.min_cushion_cents == 7_000
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/c-none") as ws:
        ws.send_json({"type": "start", "scenario_id": "easy_deal"})
        ws.receive_json()
    assert _SESSIONS[-1].scenario.min_cushion_cents == 0  # fixtures set none


@pytest.mark.parametrize("bad", [-1, MAX_MIN_CUSHION_CENTS + 1, "100", 12.5, True])
def test_invalid_start_value_is_refused(tmp_path: Path, bad: Any) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/c-bad") as ws:
        ws.send_json({"type": "start", "scenario_id": "easy_deal", "min_cushion_cents": bad})
        frame = ws.receive_json()
    assert frame["type"] == "error" and "min_cushion_cents" in frame["message"]
    assert _SESSIONS == []


def test_firm_json_validation() -> None:
    assert parse_min_cushion(None) == 0
    assert parse_min_cushion(0) == 0 and parse_min_cushion(MAX_MIN_CUSHION_CENTS) > 0
    for bad in (-5, MAX_MIN_CUSHION_CENTS + 1, "5", 1.5, False):
        with pytest.raises(ValueError):
            parse_min_cushion(bad)
    payload = copy.deepcopy(SCENARIO_TEMPLATE)
    payload["firm"]["min_cushion_cents"] = -1
    assert [e["path"] for e in scenario_payload_errors(payload)] == ["firm.min_cushion_cents"]
    assert load_scenario(Path("fixtures/scenarios/easy_deal")).min_cushion_cents == 0
