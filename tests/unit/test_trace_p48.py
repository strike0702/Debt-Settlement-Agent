"""Phase 48: who read each rep turn (``turn_trace.reader``) and the turn's notes.

The WS layer builds both from the turn's audit rows (``turn_reader`` /
``turn_notes`` in ``app.voice.ws``); the orchestrator is untouched. Covered:
the oracle / code / model readers, fallback and budget flags from the ``llm``
rows, the ack / correction / dispute / held-amount / dropped-question notes,
and that neither ever reaches the rep socket (which carries no trace).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent.orchestrator import Utterance
from app.domain.actions import Action, Intent, Phase
from app.llm.client import FakeLLM
from app.schemas.events import SERVER_EVENT_ADAPTER
from app.voice.ws import turn_notes, turn_reader
from tests.wsutil import ack_all, make_client, offline_settings, turn


def _row(rid: int, actor: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": rid, "ts": "", "actor": actor, "type": event, "payload": payload}


def _llm(rid: int, event: str = "llm_call", **meta: Any) -> dict[str, Any]:
    base = {"role": "nlu", "provider": "anthropic", "model": "claude-haiku-5-5"}
    return _row(rid, "llm", event, {**base, "failover_from": None, "cache_hit": False, **meta})


def _utt(action: Action | None = None) -> Utterance:
    return Utterance(
        sentences=[], action=action or Action(intent=Intent.ASK, next_phase=Phase.DISCOVERY)
    )


# ---------------------------------------------------------------- reader (pure)


def test_reader_names_the_model_that_answered() -> None:
    assert turn_reader([_llm(1)], nlu_mode="llm") == {
        "kind": "llm",
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "fallback": False,
        "budget_reached": False,
        "cache_hit": False,
    }


def test_reader_marks_a_fallback_after_a_failed_target() -> None:
    rows = [
        _llm(1, "llm_call_failed", error="timeout"),
        _llm(
            2,
            provider="groq",
            model="openai/gpt-oss-120b",
            failover_from="anthropic/claude-haiku-5-5",
        ),
    ]
    got = turn_reader(rows, nlu_mode="llm")
    assert got is not None
    assert (got["provider"], got["fallback"], got["budget_reached"]) == ("groq", True, False)


def test_reader_marks_the_daily_budget_skip() -> None:
    rows = [
        _llm(1, "llm_budget_exhausted", error="daily budget exhausted"),
        _llm(2, "llm_budget_skip", error="daily budget exhausted"),
        _llm(
            3,
            provider="groq",
            model="openai/gpt-oss-120b",
            failover_from="anthropic/claude-haiku-5-5",
        ),
    ]
    got = turn_reader(rows, nlu_mode="llm")
    assert (
        got is not None and got["budget_reached"] and got["fallback"] and got["provider"] == "groq"
    )


def test_reader_ignores_other_roles_and_uses_the_last_nlu_answer() -> None:
    rows = [
        _llm(1, provider="groq", model="whisper", role="stt"),
        _llm(2, provider="cerebras", model="gpt-oss-120b"),
        _llm(3, provider="groq", model="openai/gpt-oss-20b", role="nlg"),
    ]
    got = turn_reader(rows, nlu_mode="llm")
    assert got is not None and got["provider"] == "cerebras"


def test_reader_code_script_and_nothing() -> None:
    fast = [_row(1, "nlu", "fast_readback", {"response": "confirm"})]
    assert turn_reader(fast, nlu_mode="llm") == {"kind": "code"}
    assert turn_reader([_llm(1)], nlu_mode="oracle") == {"kind": "script"}
    assert turn_reader([], nlu_mode="llm") is None


# ---------------------------------------------------------------- notes (pure)


def test_notes_from_audit_rows_in_order() -> None:
    rows = [
        _row(
            1, "belief", "ack_corrected", {"field": "max_payments", "old_value": 5, "new_value": 6}
        ),
        _row(
            2,
            "nlu",
            "nlu_amount_ambiguous",
            {"cents": 42000, "quote": "pay $420 by", "trigger": "total_pay_by"},
        ),
        _row(3, "nlu", "amount_clarify_dropped", {"cents": 42000, "reason": "new_terms"}),
        _row(
            4, "nlu", "cents_clarify_dropped", {"field": "min_payment_cents", "reason": "new_terms"}
        ),
        _row(
            5, "nlu", "amount_clarify_resolved", {"total_cents": 42000, "min_payment_cents": None}
        ),
        _row(
            6, "belief", "ack_disputed", {"field": "max_payments", "old_value": 6, "new_value": 6}
        ),
        _row(7, "engine", "affordability", {"max_bp": 5200}),
    ]
    kinds = [n["kind"] for n in turn_notes(rows, _utt())]
    assert kinds == [
        "ack_corrected",
        "amount_held",
        "amount_clarify_dropped",
        "cents_clarify_dropped",
        "amount_clarify_resolved",
        "ack_disputed",
    ]
    held = turn_notes(rows, _utt())[1]
    assert held == {
        "kind": "amount_held",
        "cents": 42000,
        "quote": "pay $420 by",
        "trigger": "total_pay_by",
    }


def test_ack_note_lists_acked_fields_unless_the_ack_was_dropped() -> None:
    from app.domain.facts import Fact

    ack = {
        "ack_max_payments": Fact(
            id="ack_max_payments", kind="count", value=6, visibility="PUBLIC", source="creditor"
        ),
    }
    action = Action(intent=Intent.ASK, next_phase=Phase.DISCOVERY, ack=ack)
    assert turn_notes([], _utt(action)) == [
        {"kind": "acked", "fields": ["max_payments"], "total": None}
    ]
    dropped = [_row(1, "nlg", "act_dropped", {"act": "ack"})]
    assert turn_notes(dropped, _utt(action)) == []


# ---------------------------------------------------------------- over the socket


_FIVE = {
    "type": "text",
    "text": "Up to five payments.",
    "oracle": {
        "stance": "info",
        "terms": [{"field": "max_payments", "value": 5, "quote": "five", "hedged": False}],
    },
}
_SIX = {
    "type": "text",
    "text": "No, I said six payments.",
    "oracle": {
        "stance": "info",
        "terms": [{"field": "max_payments", "value": 6, "quote": "six", "hedged": False}],
    },
}


def _play(ws: Any, *msgs: dict[str, Any]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    ws.send_json({"type": "start", "scenario_id": "easy_deal"})
    batch = turn(ws)
    frames += batch + ack_all(ws, batch)
    for m in msgs:
        ws.send_json(m)
        batch = turn(ws)
        frames += batch + ack_all(ws, batch)
    return frames


def test_operator_trace_carries_reader_and_correction_notes(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/p48-op") as ws:
        frames = _play(ws, _FIVE, _SIX)
    traces = [f for f in frames if f["type"] == "turn_trace"]
    for f in traces:
        SERVER_EVENT_ADAPTER.validate_python(f)
    opening, first, second = traces
    assert opening.get("reader") is None and opening.get("notes") is None
    assert first["reader"] == {"kind": "script"}
    assert first["notes"] == [{"kind": "acked", "fields": ["max_payments"], "total": None}]
    kinds = [n["kind"] for n in second["notes"]]
    assert kinds[0] == "ack_corrected" and second["notes"][0]["new_value"] == 6
    assert "acked" in kinds


def test_live_nlu_turn_names_the_fake_model(tmp_path: Path) -> None:
    llm = FakeLLM()
    llm.enqueue("nlu", json.dumps(_FIVE["oracle"]))
    settings = offline_settings(nlu_mode="llm")
    with (
        make_client(tmp_path, settings=settings, llm=llm) as client,
        client.websocket_connect("/ws/call/p48-llm") as ws,
    ):
        frames = _play(ws, {"type": "text", "text": "Up to five payments."})
    rep_turn = [f for f in frames if f["type"] == "turn_trace"][-1]
    assert rep_turn["reader"]["kind"] == "llm"
    assert (rep_turn["reader"]["provider"], rep_turn["reader"]["fallback"]) == ("fake", False)


def test_rep_stream_never_carries_reader_or_notes(tmp_path: Path) -> None:
    with (
        make_client(tmp_path) as client,
        client.websocket_connect("/ws/call/p48-rep?view=rep") as ws,
    ):
        frames = _play(ws, _FIVE, _SIX)
    blob = json.dumps(frames)
    assert not any(f["type"] == "turn_trace" for f in frames)
    for key in ('"reader"', '"notes"', "ack_corrected", "claude", "anthropic"):
        assert key not in blob, key


def test_rep_stream_never_carries_the_handoff_reason_code(tmp_path: Path) -> None:
    """The rep hears the spoken handoff line; the policy's code stays with the negotiator."""
    with (
        make_client(tmp_path) as client,
        client.websocket_connect("/ws/call/p48-rep-esc?view=rep") as ws,
    ):
        start = {"type": "start", "scenario_id": "no_space", "autoplay": True}
        ws.send_json({**start, "autoplay_pause_ms": 0})
        frames: list[dict[str, Any]] = []
        while not frames or frames[-1]["type"] != "autoplay_done":
            frames.append(ws.receive_json())
    esc = [f for f in frames if f["type"] == "escalate"]
    assert esc and esc[-1]["reason"] is None
    assert "specialist" in esc[-1]["escalate_reason"]
    decide = [f for f in frames if f["type"] == "audit" and f["event"] == "decide"]
    assert decide and all("reason" not in f["payload"] for f in decide)
    assert "infeasible" not in json.dumps(frames)
