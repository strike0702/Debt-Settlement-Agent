"""Phase 21 (F4, F15): concurrent WS handlers and the latency event shape.

A ``FakeLLM`` whose NLU sleeps keeps a turn inside NLU long enough for the
test to send a second ``text`` (merged into one turn) or a ``barge_in``
(applied before the turn finishes). Template NLG, so only NLU hits the LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog

_NLU_REPLY = '{"terms": [], "stance": "info"}'


class _SlowNLU(FakeLLM):
    """FakeLLM whose ``nlu`` calls take ``delay_s`` and record their prompts."""

    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s
        self.nlu_prompts: list[str] = []

    async def chat_text(
        self, role: Any, messages: Sequence[Mapping[str, Any]], max_tokens: int
    ) -> str:
        if role == "nlu":
            self.nlu_prompts.append("\n".join(str(m.get("content")) for m in messages))
            await asyncio.sleep(self.delay_s)
        return await super().chat_text(role, messages, max_tokens)


def _settings() -> Settings:
    return Settings(
        nlu_mode="llm",
        nlg_mode="template",
        llm_profile="offline",
        db_path=":memory:",
    )


def _recv_until(ws: Any, predicate: Any, *, limit: int = 120) -> list[dict]:
    got: list[dict] = []
    for _ in range(limit):
        msg = ws.receive_json()
        got.append(msg)
        if predicate(msg):
            break
    return got


def _start(ws: Any) -> None:
    ws.send_json({"type": "start", "scenario": "fixtures/demo"})
    opening = _recv_until(ws, lambda m: m.get("type") == "turn_done")
    for ev in opening:
        if ev.get("type") == "say":
            ws.send_json({"type": "sentence_done", "id": ev["id"]})
            _recv_until(ws, lambda m: m.get("type") == "turn_done")


def _is_creditor_echo(m: dict) -> bool:
    return m.get("type") == "transcript" and m.get("role") == "creditor"


def test_second_text_during_nlu_merges_into_one_turn(tmp_path: Path) -> None:
    llm = _SlowNLU(delay_s=0.4)
    llm.enqueue("nlu", _NLU_REPLY)  # first fragment (discarded on restart)
    llm.enqueue("nlu", _NLU_REPLY)  # merged rerun
    audit = AuditLog(tmp_path / "a.db")
    app = create_app(settings=_settings(), llm=llm, audit=audit)
    with TestClient(app) as client, client.websocket_connect("/ws/call/merge-1") as ws:
        _start(ws)
        ws.send_json({"type": "text", "text": "We can do eight payments"})
        _recv_until(ws, _is_creditor_echo)  # echo is sent before NLU starts
        ws.send_json({"type": "text", "text": "with a hundred dollar minimum"})
        events: list[dict] = []
        while sum(1 for e in events if e.get("type") == "turn_done") < 2:
            events.append(ws.receive_json())

    says = [e for e in events if e["type"] == "say"]
    latencies = [e for e in events if e["type"] == "latency"]
    assert says, "merged turn spoke"
    assert len(latencies) == 1, "one agent turn for two fragments"
    assert len(llm.nlu_prompts) == 2
    assert "eight payments with a hundred dollar minimum" in llm.nlu_prompts[-1]
    types = [r["type"] for r in audit.for_call("merge-1")]
    assert "nlu_cancel_merge" in types and "nlu_restart" in types
    utterances = [r for r in audit.for_call("merge-1") if r["type"] == "turn_complete"]
    assert len(utterances) == 1


def test_barge_in_during_nlu_is_applied(tmp_path: Path) -> None:
    llm = _SlowNLU(delay_s=0.5)
    llm.enqueue("nlu", _NLU_REPLY)
    audit = AuditLog(tmp_path / "a.db")
    app = create_app(settings=_settings(), llm=llm, audit=audit)
    with TestClient(app) as client, client.websocket_connect("/ws/call/barge-1") as ws:
        _start(ws)
        ws.send_json({"type": "text", "text": "Eight payments max"})
        _recv_until(ws, _is_creditor_echo)
        ws.send_json({"type": "barge_in", "spoken_ids": []})
        events: list[dict] = []
        while sum(1 for e in events if e.get("type") == "turn_done") < 2:
            events.append(ws.receive_json())

    kinds = [e["type"] for e in events]
    # The barge-in's own turn_done arrives while the text turn is still in NLU.
    assert kinds.index("turn_done") < kinds.index("say")
    rows = audit.for_call("barge-1")
    barge = [i for i, r in enumerate(rows) if r["type"] == "barge_in"]
    analysis = [i for i, r in enumerate(rows) if r["type"] == "analysis"]
    assert barge and analysis and barge[-1] < analysis[-1]


def test_latency_event_has_engine_and_queue(tmp_path: Path) -> None:
    llm = _SlowNLU(delay_s=0.0)
    llm.enqueue("nlu", _NLU_REPLY)
    app = create_app(settings=_settings(), llm=llm, audit=AuditLog(tmp_path / "a.db"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/call/lat-1") as ws:
            _start(ws)
            ws.send_json({"type": "text", "text": "Eight payments max"})
            events = _recv_until(ws, lambda m: m.get("type") == "turn_done")
        latency = next(e for e in events if e["type"] == "latency")
        for key in (
            "stt_ms",
            "nlu_ms",
            "engine_ms",
            "policy_ms",
            "nlg_ms",
            "queue_ms",
            "server_total_ms",
        ):
            assert key in latency
        assert isinstance(latency["engine_ms"], float)
        assert latency["queue_ms"] == 0.0  # FakeLLM never waits on a limiter
        summary = client.get("/metrics/summary").json()
        assert {"engine_ms", "queue_ms"} <= set(summary)
        assert summary["engine_ms"]["n"] >= 1 and summary["queue_ms"]["n"] >= 1
