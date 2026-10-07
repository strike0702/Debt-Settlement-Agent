"""Phase 36: ``GET /calls/{id}/operator`` backfills the Debt negotiator view.

A call streamed on ``?view=rep`` never carries ``turn_trace`` (or private audit
rows, fees, ``max_bp``) on its socket. The server keeps each call's operator
detail in memory so the web app can still show the decision trace, ladder,
latency and audit log when the user switches to the Debt negotiator view.
The rep stream itself must stay exactly as filtered as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.schemas.events import SERVER_EVENT_ADAPTER
from app.voice import ws as ws_mod
from tests.wsutil import make_client, scripted_easy_deal


def _autoplay(ws: Any, scenario_id: str) -> list[dict]:
    ws.send_json(
        {"type": "start", "scenario_id": scenario_id, "autoplay": True, "autoplay_pause_ms": 0}
    )
    frames: list[dict] = []
    while not frames or frames[-1]["type"] != "autoplay_done":
        frames.append(ws.receive_json())
    return frames


def test_rep_view_call_keeps_operator_traces_on_the_server(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        with client.websocket_connect("/ws/call/p36-rep?view=rep") as ws:
            frames = _autoplay(ws, "easy_deal")
        detail = client.get("/calls/p36-rep/operator")
    # The rep stream is unchanged: no trace, no private audit row.
    assert not any(f["type"] == "turn_trace" for f in frames)
    assert detail.status_code == 200
    body = detail.json()
    assert body["call_id"] == "p36-rep"
    traces = body["traces"]
    turns = [t["turn"] for t in traces]
    assert turns == sorted(turns) and len(turns) == len(set(turns)) >= 4
    assert any(t.get("affordability") for t in traces)
    for t in traces:
        SERVER_EVENT_ADAPTER.validate_python(t)
    # Audit rows come in the WS shape with the privacy flag, so the UI folds them as-is.
    assert any(a["private"] and a["actor"] == "engine" for a in body["audit"])
    for a in body["audit"]:
        SERVER_EVENT_ADAPTER.validate_python(a)
    assert body["eval"] is not None and "max_bp" in body["eval"]
    assert body["agreement"] is not None
    assert "program_fee_cents" in body["agreement"]["rows"][0]


def test_operator_detail_matches_an_operator_stream(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        with client.websocket_connect("/ws/call/p36-op") as ws:
            frames = scripted_easy_deal(ws)
        body = client.get("/calls/p36-op/operator").json()
    streamed = {f["turn"]: f for f in frames if f["type"] == "turn_trace"}
    assert {t["turn"]: t for t in body["traces"]} == streamed


def test_operator_detail_resets_on_a_new_start(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        with client.websocket_connect("/ws/call/p36-again?view=rep") as ws:
            _autoplay(ws, "easy_deal")
            first = client.get("/calls/p36-again/operator").json()
            _autoplay(ws, "no_space")
            second = client.get("/calls/p36-again/operator").json()
    assert first["agreement"] is not None
    assert second["agreement"] is None
    assert second["traces"] and second["traces"] != first["traces"]


def test_operator_detail_unknown_call_is_404(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/calls/never-started/operator").status_code == 404


def test_operator_detail_store_is_bounded(monkeypatch: Any) -> None:
    monkeypatch.setattr(ws_mod, "_DETAIL_MAX_CALLS", 3)
    monkeypatch.setattr(ws_mod, "_DETAIL", type(ws_mod._DETAIL)())
    for i in range(5):
        ws_mod._reset_operator_detail(f"c{i}")
    assert list(ws_mod._DETAIL) == ["c2", "c3", "c4"]
    assert ws_mod.operator_detail("c0") is None
