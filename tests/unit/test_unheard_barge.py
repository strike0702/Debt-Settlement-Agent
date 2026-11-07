"""A reply the rep never heard leaves no trace (live call 476d2eb4, 2026-10-27).

Replays the evidence on the demo scenario with oracle NLU: the rep pauses
mid-sentence ("I have to take around"), the agent answers with a hold, and the
rest of the sentence ("I mean seventy percent.") arrives before any of that
hold was heard. Expected: the server drops the hold, tells the client which
sentences to cancel, rolls the ladder back to before that turn, merges the two
fragments into one rep line (one NLU pass), and decides once (a hold, not a
step). Late acks of the dropped sentences are ignored and audited.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.orchestrator import Orchestrator
from app.agent.policy import Intent
from app.agent.session import CallSession
from app.config import Settings
from app.domain.nlu_types import RepTurn
from app.domain.scenario import load_scenario
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog
from tests.acts_helpers import acts_turn, rule_act

_ASK = acts_turn(settlement_ask_pct=70.0, ask_quote="seventy percent", stance="counter")
_FRAGMENT = acts_turn(stance="other")
_TERMS_TEXT = "Max six payments, minimum payment twenty five dollars, even payments."
_TERMS = acts_turn(
    stance="info",
    terms=[
        rule_act(field="max_payments", value=6, quote="six"),
        rule_act(field="min_payment_cents", value=2500, quote="twenty five dollars"),
        rule_act(field="payment_structure", value="even", quote="even"),
    ],
)


def _orch(tmp_path: Path) -> tuple[Orchestrator, list[list[str]]]:
    settings = Settings(
        nlu_mode="oracle",
        nlg_mode="template",
        llm_profile="offline",
        max_turns=24,
        max_counters=6,
        anchor_ratio=0.7,
        concession_factor=0.5,
    )
    session = CallSession(scenario=load_scenario("fixtures/demo"))
    orch = Orchestrator(
        session, llm=None, settings=settings, audit=AuditLog(tmp_path / "a.db"), auto_ack=False
    )
    dropped: list[list[str]] = []

    async def on_dropped(ids: list[str]) -> None:
        dropped.append(list(ids))

    orch.on_speech_dropped = on_dropped
    return orch, dropped


def _events(orch: Orchestrator, kind: str) -> list[dict[str, Any]]:
    return [e for e in orch.audit.for_call(orch.session.call_id) if e["type"] == kind]


async def _heard(orch: Orchestrator, text: str, oracle: RepTurn) -> Any:
    u = await orch.on_creditor_text(text, oracle=oracle)
    await orch.on_sentence_done([sid for sid, _ in u.sentences])
    return u


async def _to_first_counter(orch: Orchestrator) -> int:
    u = await orch.start()
    await orch.on_sentence_done([sid for sid, _ in u.sentences])
    await _heard(orch, _TERMS_TEXT, _TERMS)
    u = await _heard(orch, "We need seventy percent.", _ASK)
    assert u.action.intent == Intent.COUNTER and u.action.reason != "hold"
    return int(u.action.facts["counter_pct"].value)


def _ladder(orch: Orchestrator) -> tuple[Any, ...]:
    n = orch.session.neg
    return (
        list(n.counters_offered),
        n.hold_stage,
        list(n.counter_turns),
        n.ask_at_last_counter,
        n.final_counter_ask,
        n.ask_bp,
        list(n.ask_history),
        n.turn_idx,
    )


@pytest.mark.asyncio
async def test_unheard_hold_is_dropped_merged_and_re_decided_as_a_hold(tmp_path: Path) -> None:
    orch, dropped = _orch(tmp_path)
    anchor = await _to_first_counter(orch)
    before = _ladder(orch)

    hold = await orch.on_creditor_text("I have to take around", oracle=_FRAGMENT)
    assert hold.action.reason == "hold"
    hold_ids = [sid for sid, _ in hold.sentences]

    # The rest of the sentence arrives before any of the hold was heard.
    u = await orch.on_creditor_text("I mean seventy percent.", oracle=_ASK)

    assert dropped == [hold_ids]
    (barge,) = [b for b in _events(orch, "barge_in") if b["payload"].get("dropped_intent")]
    assert barge["payload"]["spoken_ids"] == []
    assert barge["payload"]["rolled_back"] is True
    # One rep line, one decision: still a hold at the anchor (not a step).
    assert u.action.intent == Intent.COUNTER and u.action.reason == "hold"
    assert u.action.facts["counter_pct"].value == anchor
    merged = _events(orch, "utterance")[-1]
    assert merged["payload"]["text"] == "I have to take around I mean seventy percent."
    assert merged["payload"]["turn"] == before[-1] + 1
    rep_lines = [t.text for t in orch.session.history if t.role == "creditor"]
    assert rep_lines[-1] == "I have to take around I mean seventy percent."
    assert "I have to take around" not in rep_lines[:-1]

    # A late ack of a dropped sentence is ignored (no ack, no effects) and audited.
    stage = orch.session.neg.hold_stage
    await orch.on_sentence_done([hold_ids[0]])
    assert orch.session.pending is not None and orch.session.pending.acked == set()
    assert orch.session.neg.hold_stage == stage
    (ignored,) = _events(orch, "sentence_done_ignored")
    assert ignored["payload"] == {"ids": [hold_ids[0]], "reason": "dropped"}


@pytest.mark.asyncio
async def test_barging_a_counter_with_nothing_heard_never_advances_the_ladder(
    tmp_path: Path,
) -> None:
    """Invariant: anchor → hold → step → step, each barged unheard once first."""
    orch, _ = _orch(tmp_path)
    await _to_first_counter(orch)
    reasons: list[str] = []
    for _ in range(3):
        before = _ladder(orch)
        u = await orch.on_creditor_text("Seventy percent.", oracle=_ASK)
        if u.action.intent != Intent.COUNTER:
            break
        await orch.on_barge_in([])
        assert _ladder(orch) == before, u.action.reason
        assert orch.session.pending is None
        # Said again and heard: the same move, now counted once.
        again = await _heard(orch, "Seventy percent.", _ASK)
        assert again.action.reason == u.action.reason
        assert again.action.facts["counter_pct"].value == u.action.facts["counter_pct"].value
        reasons.append(str(u.action.reason))
    assert reasons == ["hold", "step", "step"]


@pytest.mark.asyncio
async def test_a_heard_counter_still_keeps_its_bookkeeping(tmp_path: Path) -> None:
    orch, dropped = _orch(tmp_path)
    anchor = await _to_first_counter(orch)
    hold = await orch.on_creditor_text("Seventy percent.", oracle=_ASK)
    await orch.on_barge_in([hold.sentences[0][0]])
    assert orch.session.neg.hold_stage == 1
    assert orch.session.neg.counters_offered == [anchor]
    # Only the unheard rest is cancelled on the client.
    assert dropped == [[sid for sid, _ in hold.sentences[1:]]]
    # The rep heard it, so the next line is not merged into the barged one.
    u = await orch.on_creditor_text("Seventy percent.", oracle=_ASK)
    assert u.action.reason == "step"
    assert _events(orch, "utterance")[-1]["payload"]["text"] == "Seventy percent."


# ---------------------------------------------------------------- socket


def _recv_until(ws: Any, kind: str, *, limit: int = 120) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []
    for _ in range(limit):
        got.append(ws.receive_json())
        if got[-1].get("type") == kind:
            break
    return got


def _send(ws: Any, text: str, oracle: RepTurn) -> list[dict[str, Any]]:
    ws.send_json({"type": "text", "text": text, "oracle": oracle.model_dump(mode="json")})
    return _recv_until(ws, "turn_done")


def _ack(ws: Any, frames: list[dict[str, Any]]) -> None:
    for f in frames:
        if f["type"] == "say":
            ws.send_json({"type": "sentence_done", "id": f["id"]})
            _recv_until(ws, "turn_done")


def test_socket_tells_the_client_to_cancel_the_unheard_hold_before_the_new_reply(
    tmp_path: Path,
) -> None:
    settings = Settings(
        nlu_mode="oracle", nlg_mode="template", llm_profile="offline", db_path=":memory:"
    )
    audit = AuditLog(tmp_path / "ws.db")
    app = create_app(settings=settings, llm=FakeLLM(), audit=audit)
    with TestClient(app) as client, client.websocket_connect("/ws/call/unheard?view=rep") as ws:
        ws.send_json({"type": "start", "scenario": "fixtures/demo"})
        _ack(ws, _recv_until(ws, "turn_done"))
        _ack(ws, _send(ws, _TERMS_TEXT, _TERMS))
        _ack(ws, _send(ws, "We need seventy percent.", _ASK))
        held = _send(ws, "I have to take around", _FRAGMENT)
        hold = [f["id"] for f in held if f["type"] == "say"]
        frames = _send(ws, "I mean seventy percent.", _ASK)
        kinds = [f["type"] for f in frames]
        assert {"type": "speech_dropped", "ids": hold} in frames
        assert kinds.index("speech_dropped") < kinds.index("say")
        # The client acks a dropped sentence before it hears of the drop: ignored.
        ws.send_json({"type": "sentence_done", "id": hold[0]})
        _recv_until(ws, "turn_done")
    rows = [e for e in audit.for_call("unheard") if e["type"] == "sentence_done_ignored"]
    assert [r["payload"]["ids"] for r in rows] == [[hold[0]]]
    decides = [e["payload"]["reason"] for e in audit.for_call("unheard") if e["type"] == "decide"]
    assert decides[-2:] == ["hold", "hold"]
    audit.close()
