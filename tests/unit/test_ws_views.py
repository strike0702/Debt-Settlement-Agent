"""Role-scoped WS streams (F7): ``?view=rep`` never carries private values."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app import autoplay as autoplay_mod
from app.agent.session import CallSession
from app.voice import ws as ws_mod
from tests.wsutil import leaked_private_values, make_client, scripted_easy_deal

_SESSIONS: list[CallSession] = []


class _RecordingSession(CallSession):
    def __post_init__(self) -> None:
        super().__post_init__()
        _SESSIONS.append(self)


@pytest.fixture(autouse=True)
def _record_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    _SESSIONS.clear()
    monkeypatch.setattr(ws_mod, "CallSession", _RecordingSession)
    monkeypatch.setattr(autoplay_mod, "CallSession", _RecordingSession)


def _scenario_private(session: CallSession) -> set[tuple[str, int | date]]:
    """Blocklist plus scenario-level private amounts the blocklist does not seed."""
    sc = session.scenario
    extra: set[tuple[str, int | date]] = {
        ("money", sc.bank_fee_cents),
        ("pct", round(sc.program_fee_pct * 10000)),
        ("money", round(sc.creditor_balance_cents * sc.program_fee_pct)),
    }
    if session.last_max_bp is not None:
        extra.add(("pct", session.last_max_bp))
    return set(session.private_blocklist) | {p for p in extra if p[1]}


def _autoplay(ws: Any, scenario_id: str) -> list[dict]:
    ws.send_json(
        {"type": "start", "scenario_id": scenario_id, "autoplay": True, "autoplay_pause_ms": 0}
    )
    frames: list[dict] = []
    while not frames or frames[-1]["type"] != "autoplay_done":
        frames.append(ws.receive_json())
    return frames


def test_rep_view_scripted_call_leaks_no_private_value(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/rep-1?view=rep") as ws:
        frames = scripted_easy_deal(ws)
    session = _SESSIONS[-1]
    assert any(f["type"] == "agreement" for f in frames)
    private = _scenario_private(session)
    assert len(private) > 5
    assert leaked_private_values(frames, private, ref=session.scenario.client.as_of_date) == []
    for f in frames:
        assert f["type"] != "turn_trace" or "affordability" not in f
        assert f["type"] != "eval" or not {"max_bp", "program_fee_cents", "additional_funds"} & set(
            f
        )
        assert f["type"] != "audit" or f["actor"] not in ("engine", "agent")


@pytest.mark.parametrize("scenario_id", ["easy_deal", "no_space", "rescue_escalate"])
def test_rep_view_autoplay_leaks_no_private_value(tmp_path: Path, scenario_id: str) -> None:
    with (
        make_client(tmp_path) as client,
        client.websocket_connect(f"/ws/call/rep-{scenario_id}?view=rep") as ws,
    ):
        frames = _autoplay(ws, scenario_id)
    session = _SESSIONS[-1]
    private = _scenario_private(session)
    assert leaked_private_values(frames, private, ref=session.scenario.client.as_of_date) == []


def test_operator_view_is_default_and_keeps_private_panels(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/op-1") as ws:
        frames = scripted_easy_deal(ws)
    session = _SESSIONS[-1]
    assert any(f["type"] == "eval" and "max_bp" in f for f in frames)
    assert any(f["type"] == "turn_trace" and f.get("affordability") for f in frames)
    assert any(f["type"] == "audit" and f["actor"] == "engine" and f["private"] for f in frames)
    # Sanity: the scan does find private values on the operator stream.
    assert leaked_private_values(
        frames, _scenario_private(session), ref=session.scenario.client.as_of_date
    )


def test_unknown_view_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/x?view=admin") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "view" in msg["message"]
