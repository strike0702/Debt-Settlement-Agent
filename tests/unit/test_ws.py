"""WebSocket call protocol tests (text mode, oracle NLU, template NLG)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog


def _settings() -> Settings:
    return Settings(
        nlu_mode="oracle",
        nlg_mode="template",
        llm_profile="offline",
        hostility_threshold=0.8,
        max_turns=24,
        max_counters=4,
        anchor_ratio=0.7,
        concession_factor=0.5,
        firm_name="Synthetic Debt Relief",
        opening_disclosure="This call uses synthetic data for demonstration only.",
        db_path=":memory:",
    )


def _client(tmp_path: Path) -> TestClient:
    audit = AuditLog(tmp_path / "ws_audit.db")
    app = create_app(settings=_settings(), llm=FakeLLM(), audit=audit)
    return TestClient(app)


def _recv_until(ws, predicate, *, limit: int = 80) -> list[dict]:
    got: list[dict] = []
    for _ in range(limit):
        raw = ws.receive_json()
        got.append(raw)
        if predicate(raw):
            break
    return got


def _ack_all_says(ws, events: list[dict]) -> None:
    for ev in events:
        if ev.get("type") != "say":
            continue
        ws.send_json({"type": "sentence_done", "id": ev["id"]})
        _recv_until(ws, lambda m: m.get("type") == "turn_done")


def test_ws_start_text_say_sentence_done_barge_in(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        with client.websocket_connect("/ws/call/test-call-1") as ws:
            ws.send_json({"type": "start", "scenario": "fixtures/demo"})
            opening = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            assert any(m["type"] == "say" for m in opening)
            assert any(m["type"] == "belief" for m in opening)
            assert any(m["type"] == "latency" for m in opening)
            _ack_all_says(ws, opening)

            ws.send_json(
                {
                    "type": "text",
                    "text": (
                        "Max eight payments, minimum one hundred dollars, "
                        "even payments please."
                    ),
                    "oracle": {
                        "terms": [
                            {
                                "field": "max_payments",
                                "value": 8,
                                "quote": "eight",
                                "hedged": False,
                            },
                            {
                                "field": "min_payment_cents",
                                "value": 10000,
                                "quote": "one hundred dollars",
                                "hedged": False,
                            },
                            {
                                "field": "payment_structure",
                                "value": "even",
                                "quote": "even",
                                "hedged": False,
                            },
                        ],
                        "stance": "info",
                    },
                }
            )
            turn1 = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            assert any(m["type"] == "transcript" and m["role"] == "creditor" for m in turn1)
            # Creditor line must arrive before the agent say (early echo for UI).
            cred_i = next(
                i
                for i, m in enumerate(turn1)
                if m["type"] == "transcript" and m["role"] == "creditor"
            )
            say_i = next(i for i, m in enumerate(turn1) if m["type"] == "say")
            assert cred_i < say_i
            assert any(m["type"] == "say" for m in turn1)
            assert any(m["type"] == "belief" for m in turn1)

            # Barge-in before acking the ask-settlement line.
            ws.send_json({"type": "barge_in", "spoken_ids": []})
            barge_events = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            assert any(m.get("type") == "phase" for m in barge_events)

            # Re-send rules after barge (pending dropped); then ask 45%.
            ws.send_json(
                {
                    "type": "text",
                    "text": (
                        "Max eight payments, minimum one hundred dollars, "
                        "even payments please."
                    ),
                    "oracle": {
                        "terms": [
                            {
                                "field": "max_payments",
                                "value": 8,
                                "quote": "eight",
                                "hedged": False,
                            },
                            {
                                "field": "min_payment_cents",
                                "value": 10000,
                                "quote": "one hundred dollars",
                                "hedged": False,
                            },
                            {
                                "field": "payment_structure",
                                "value": "even",
                                "quote": "even",
                                "hedged": False,
                            },
                        ],
                        "stance": "info",
                    },
                }
            )
            turn2 = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            _ack_all_says(ws, turn2)

            ws.send_json(
                {
                    "type": "text",
                    "text": "We are looking for a forty five percent settlement.",
                    "oracle": {
                        "settlement_ask_pct": 45.0,
                        "ask_quote": "forty five percent",
                        "stance": "offer",
                    },
                }
            )
            turn3 = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            assert any(m["type"] == "say" for m in turn3)
            assert any(m["type"] == "eval" and m.get("feasible") is True for m in turn3)
            eval_msg = next(m for m in turn3 if m["type"] == "eval")
            assert "max_bp" in eval_msg
            assert "offer_total_cents" in eval_msg
            _ack_all_says(ws, turn3)


def test_ws_end_closes_without_unknown_event(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        with client.websocket_connect("/ws/call/test-call-end") as ws:
            ws.send_json({"type": "start", "scenario": "fixtures/demo"})
            opening = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            _ack_all_says(ws, opening)

            ws.send_json({"type": "end"})
            ended = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            assert not any(
                m.get("type") == "error" and "unknown event" in str(m.get("message", ""))
                for m in ended
            )
            assert any(m.get("type") == "say" for m in ended)
            assert any(
                m.get("type") == "phase" and m.get("intent") == "NO_DEAL_WRAP"
                for m in ended
            )
            # Effects (set_phase END) apply on sentence_done; collect that batch.
            ack_batch: list[dict] = []
            for ev in ended:
                if ev.get("type") != "say":
                    continue
                ws.send_json({"type": "sentence_done", "id": ev["id"]})
                ack_batch.extend(
                    _recv_until(ws, lambda m: m.get("type") == "turn_done")
                )
            assert any(
                m.get("type") == "phase" and m.get("phase") == "END" for m in ack_batch
            )


def test_scenario_brief_endpoint(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.get("/scenarios/easy_deal")
        assert res.status_code == 200
        body = res.json()
        assert body["title"] == "Easy deal"
        assert body["expected"] == "deal"
        assert body["creditor"]["creditor_balance_cents"] == 125_000
        assert body["client"]["draft_amount_cents"] == 22_000
        assert body["client"]["upcoming_drafts"] == 8
        assert body["client"]["upcoming_deposits_cents"] == 176_000
        assert body["client"]["upcoming_withdrawals_cents"] == 0
        ledger = body["client"]["upcoming_ledger"]
        assert len(ledger) == 8
        assert all(e["type"] == "credit" for e in ledger)
        assert ledger[0]["amount_cents"] == 22_000
        assert body["firm"] == {
            "program_fee_bp": 1800,
            "program_fee_cents": 28_800,
            "bank_fee_cents": 950,
        }
        assert "rep_card" not in body
        assert client.get("/scenarios/nope").status_code == 404

        tpl = client.get("/scenarios/template")
        assert tpl.status_code == 200
        assert "client" in tpl.json() and "offer" in tpl.json()

        preview = client.post("/scenarios/preview", json=tpl.json())
        assert preview.status_code == 200
        assert preview.json()["creditor"]["creditor_balance_cents"] == 125_000

        bad = client.post("/scenarios/preview", json={"offer": {}})
        assert bad.status_code == 400

    from app.domain.scenario import scenario_details

    with pytest.raises(ValueError):
        scenario_details("..")


def test_ws_custom_scenario_payload(tmp_path: Path) -> None:
    from app.domain.scenario import SCENARIO_TEMPLATE

    with _client(tmp_path) as client:
        with client.websocket_connect("/ws/call/custom-1") as ws:
            ws.send_json(
                {
                    "type": "start",
                    "scenario_id": "custom",
                    "scenario_payload": SCENARIO_TEMPLATE,
                }
            )
            opening = _recv_until(ws, lambda m: m.get("type") == "turn_done")
            assert any(m["type"] == "say" for m in opening)
            assert any(m["type"] == "belief" for m in opening)


def test_metrics_summary_shape(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        res = client.get("/metrics/summary")
        assert res.status_code == 200
        body = res.json()
        assert "server_total_ms" in body
        assert "p50" in body["server_total_ms"]
        assert "p95" in body["server_total_ms"]
        assert "n" in body["server_total_ms"]


@pytest.mark.asyncio
async def test_stt_wrapper_timing() -> None:
    from app.voice.stt import transcribe

    fake = FakeLLM()
    fake.enqueue("stt", "hello from whisper")
    text, ms = await transcribe(fake, b"RIFF....")
    assert text == "hello from whisper"
    assert ms >= 0
