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
    # Phase 35: the decision trace is the negotiator's tool; the rep stream has none.
    assert not any(f["type"] == "turn_trace" for f in frames)
    for f in frames:
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
    # Every ledger amount the negotiator's ledger table shows is in the scan.
    assert {("money", e.amount_cents) for e in session.scenario.client.ledger} <= private
    assert not any(f["type"] == "turn_trace" for f in frames)
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


def test_rep_http_export_and_events_leak_no_private_value(tmp_path: Path) -> None:
    """[22.1] ``/calls/{id}/export?view=rep`` and ``/events?view=rep`` drop private rows."""
    with make_client(tmp_path) as client:
        with client.websocket_connect("/ws/call/http-rep?view=rep") as ws:
            frames = scripted_easy_deal(ws)
        session = _SESSIONS[-1]
        private = _scenario_private(session)
        ref = session.scenario.client.as_of_date
        op = client.get("/calls/http-rep/export").json()
        rep = client.get("/calls/http-rep/export?view=rep").json()
        rep_events = client.get("/calls/http-rep/events?view=rep").json()
        assert client.get("/calls/http-rep/events?view=nope").status_code == 400
    # The rep-view frames give the scan what the rep said and heard (its exemptions);
    # they are clean on their own (test above), so any hit comes from the HTTP body.
    assert leaked_private_values([*frames, op], private, ref=ref) != []
    assert leaked_private_values([*frames, rep], private, ref=ref) == []
    assert leaked_private_values([*frames, *rep_events], private, ref=ref) == []
    assert rep["view"] == "rep" and len(rep["events"]) < len(op["events"])
    assert {e["actor"] for e in rep["events"]}.isdisjoint({"engine", "agent", "llm"})


def test_rep_audit_filter_is_an_allow_list() -> None:
    """[P22] An audit event nobody has reviewed stays off the rep stream."""
    from app.voice.views import is_private_audit

    assert not is_private_audit("creditor", "utterance")
    assert not is_private_audit("policy", "decide")
    for actor, event in [
        ("engine", "affordability"),
        ("agent", "agreement_drafted"),
        ("llm", "call"),
        ("nlg", "blocked"),
        ("nlu", "llm_unavailable"),
        ("orchestrator", "effects_committed"),
        ("orchestrator", "barge_in"),
        # Future events, unknown actors: private by default.
        ("nlu", "some_new_event"),
        ("policy", "debug_dump"),
        ("newcomer", "utterance"),
    ]:
        assert is_private_audit(actor, event), (actor, event)


_STALL = {"type": "text", "text": "Let me pull up the account.", "oracle": {"stance": "stall"}}


def test_operator_trace_says_why_the_engine_did_not_run(tmp_path: Path) -> None:
    """[P35] Turns before the rules are known carry ``needs_info``; later turns the curve."""
    from tests.wsutil import _RULES, ack_all, turn

    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/ni-1") as ws:
        ws.send_json({"type": "start", "scenario_id": "easy_deal"})
        frames = turn(ws)
        frames += ack_all(ws, frames)
        for msg in (_STALL, _RULES):
            ws.send_json(msg)
            batch = turn(ws)
            frames += batch + ack_all(ws, batch)
    traces = {f["turn"]: f for f in frames if f["type"] == "turn_trace"}
    assert "needs_info" not in traces[0] or traces[0]["needs_info"] is None  # opening line
    assert traces[1]["affordability"] is None
    assert traces[1]["needs_info"] == ["max_payments", "min_payment_cents", "payment_structure"]
    assert traces[2]["affordability"] and traces[2]["affordability"]["curve"]
    assert traces[2].get("needs_info") is None
