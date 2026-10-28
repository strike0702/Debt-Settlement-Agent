"""Why-infeasible on the decision trace (Phase 60): operator only, never NLG.

When the rep's ask has no payment plan, the orchestrator asks ``feasibility.explain``
why and puts the answer on ``TurnTrace.infeasible_reason`` (reason, date, shortfall).
It is the client's finances: the operator stream carries it, the rep stream never does
(no ``turn_trace``, and ``views.redact_for_view`` strips the key by name), the
shortfall joins the guard blocklist the leak scan uses, and no spoken move carries it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app import autoplay as autoplay_mod
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.voice import ws as ws_mod
from app.voice.views import redact_for_view
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


def _autoplay(ws: Any, scenario_id: str) -> list[dict]:
    ws.send_json(
        {"type": "start", "scenario_id": scenario_id, "autoplay": True, "autoplay_pause_ms": 0}
    )
    frames: list[dict] = []
    while not frames or frames[-1]["type"] not in ("autoplay_done", "error"):
        frames.append(ws.receive_json())
    return frames


def _reasons(frames: list[dict]) -> list[dict]:
    return [
        f["infeasible_reason"]
        for f in frames
        if f["type"] == "turn_trace" and f.get("infeasible_reason")
    ]


@pytest.mark.parametrize(
    ("scenario_id", "reason", "shortfall"),
    [
        ("rescue_escalate", "fee_short", 17_500),
        ("no_space", "floors", 3_000),
        ("late_start_date", "horizon", None),
    ],
)
def test_operator_trace_says_why_the_ask_does_not_fit(
    tmp_path: Path, scenario_id: str, reason: str, shortfall: int | None
) -> None:
    with (
        make_client(tmp_path) as client,
        client.websocket_connect(f"/ws/call/i-{scenario_id}") as ws,
    ):
        frames = _autoplay(ws, scenario_id)
    marks = _reasons(frames)
    assert marks and marks[0]["reason"] == reason
    assert marks[0]["shortfall_cents"] == shortfall
    assert marks[0]["binding_date"]
    rows = [f for f in frames if f["type"] == "audit" and f["event"] == "infeasible"]
    assert rows and all(r["private"] and r["actor"] == "engine" for r in rows)
    session = _SESSIONS[-1]
    if shortfall is not None:
        assert ("money", shortfall) in session.private_blocklist
    # NLG never receives it: no fact on any spoken move carries the shortfall or the reason.
    assert _SPOKEN
    for action in _SPOKEN:
        if shortfall is not None:
            assert all(f.value != shortfall for f in action.facts.values())
        assert not any("infeasible" in fid or "shortfall" in fid for fid in action.facts)
        assert "TraceInfeasible" not in repr(action) and "shortfall" not in repr(action)


def test_easy_deal_trace_has_no_explanation(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/i-easy") as ws:
        frames = _autoplay(ws, "easy_deal")
    assert any(f["type"] == "turn_trace" for f in frames)
    assert _reasons(frames) == []


def test_rep_stream_never_carries_the_explanation(tmp_path: Path) -> None:
    with (
        make_client(tmp_path) as client,
        client.websocket_connect("/ws/call/i-rep?view=rep") as ws,
    ):
        frames = _autoplay(ws, "rescue_escalate")
    session = _SESSIONS[-1]
    assert ("money", 17_500) in session.private_blocklist
    dump = json.dumps(frames)
    assert "infeasible" not in dump and "shortfall" not in dump
    assert leaked_private_values(
        frames, set(session.private_blocklist), ref=session.scenario.client.as_of_date
    ) == []


def test_views_strip_the_field_by_name() -> None:
    why = {"reason": "cash_short", "bp": 5200, "binding_date": "2027-03-31",
           "shortfall_cents": 14_000, "closest_k": 3}
    trace = {"type": "turn_trace", "turn": 2, "infeasible_reason": why}
    assert redact_for_view(trace, "rep") is None
    assert redact_for_view(trace, "operator") == trace
    # Any other frame that ever carried it loses the key on the rep stream only.
    frame = {"type": "eval", "rows": None, "infeasible_reason": why}
    rep = redact_for_view(frame, "rep")
    assert rep is not None and "infeasible_reason" not in rep
    assert redact_for_view(frame, "operator") == frame
    audit = {"type": "audit", "actor": "engine", "event": "infeasible", "payload": why}
    assert redact_for_view(audit, "rep") is None
