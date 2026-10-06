"""``Utterance.trace`` / WS ``turn_trace``: one per turn, built from turn data."""

from __future__ import annotations

from pathlib import Path

from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.domain.scenario import load_scenario
from app.store.audit import AuditLog
from tests.wsutil import make_client, offline_settings, scripted_easy_deal


def _orch(tmp_path: Path, **kw) -> Orchestrator:
    session = CallSession(scenario=load_scenario("fixtures/scenarios/easy_deal"))
    return Orchestrator(
        session,
        llm=None,
        settings=offline_settings(**kw),
        audit=AuditLog(tmp_path / "a.db"),
        auto_ack=True,
    )


async def test_opening_trace_has_no_creditor_side(tmp_path: Path) -> None:
    utt = await _orch(tmp_path).start()
    tr = utt.trace
    assert tr is not None
    assert tr.creditor_text is None and tr.terms == [] and tr.affordability is None
    assert tr.decide.intent == "OPENING"
    assert tr.decide.reason_text
    assert tr.nlg.mode == "template" and "{firm_name}" in tr.nlg.template
    assert [s.text for s in tr.spoken] == [t for _, t in utt.sentences]


async def test_trace_records_verified_dropped_belief_and_engine(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    await orch.start()
    text = "Max eight payments, minimum one hundred dollars, even payments please."
    oracle = TurnAnalysis(
        stance="info",
        terms=[
            ExtractedTerm(field="max_payments", value=8, quote="eight"),
            ExtractedTerm(field="min_payment_cents", value=10000, quote="one hundred dollars"),
            ExtractedTerm(field="payment_structure", value="even", quote="even"),
            # Hallucinated: the quote is not in the utterance → dropped by post_verify.
            ExtractedTerm(field="max_segments", value=3, quote="three segments"),
        ],
    )
    utt = await orch.on_creditor_text(text, oracle=oracle)
    tr = utt.trace
    assert tr is not None
    assert tr.creditor_text == text
    assert {t.field for t in tr.terms} == {"max_payments", "min_payment_cents", "payment_structure"}
    assert all(t.verified and not t.hedged for t in tr.terms)
    assert [(d.field, d.reason) for d in tr.dropped] == [("max_segments", "rejected_quote")]
    assert {c.field for c in tr.belief_changes} >= {"max_payments", "min_payment_cents"}
    assert tr.decide.intent == "ASK_SETTLEMENT"
    # Rules are known → affordability ran (operator data).
    assert tr.affordability is not None and len(tr.affordability.curve) == 100
    assert tr.affordability.curve[0].bp == 100 and tr.affordability.curve[-1].bp == 10000

    utt = await orch.on_creditor_text(
        "We are looking for a forty five percent settlement.",
        oracle=TurnAnalysis(
            stance="offer", settlement_ask_pct=45.0, ask_quote="forty five percent"
        ),
    )
    tr = utt.trace
    assert tr is not None
    assert tr.ask_bp == 4500 and tr.ask_quote == "forty five percent"
    assert tr.decide.intent in ("COUNTER", "CONFIRM_SCHEDULE")
    assert tr.counter_bp is not None
    assert "{" not in tr.decide.reason_text
    assert tr.nlg.guards and all(g.ok for g in tr.nlg.guards)
    assert tr.nlg.fallback_used is False
    assert set(tr.timings) >= {"nlu_ms", "engine_ms", "policy_ms", "nlg_ms", "server_total_ms"}


async def test_bank_mode_trace_names_bank_template(tmp_path: Path) -> None:
    orch = _orch(tmp_path, nlg_mode="bank")
    await orch.start()
    await orch.on_creditor_text(
        "Max eight payments, minimum one hundred dollars, even payments please.",
        oracle=TurnAnalysis(
            stance="info",
            terms=[
                ExtractedTerm(field="max_payments", value=8, quote="eight"),
                ExtractedTerm(field="min_payment_cents", value=10000, quote="one hundred dollars"),
                ExtractedTerm(field="payment_structure", value="even", quote="even"),
            ],
        ),
    )
    utt = await orch.on_creditor_text(
        "We are looking for a ninety five percent settlement.",
        oracle=TurnAnalysis(
            stance="offer", settlement_ask_pct=95.0, ask_quote="ninety five percent"
        ),
    )
    tr = utt.trace
    assert tr is not None and tr.decide.intent == "COUNTER"
    assert tr.nlg.mode == "bank" and tr.nlg.source == "bank"
    assert "{counter_pct}" in tr.nlg.template


def test_ws_emits_one_turn_trace_per_turn(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/trace-1") as ws:
        frames = scripted_easy_deal(ws)
    traces = [f for f in frames if f["type"] == "turn_trace"]
    latencies = [f for f in frames if f["type"] == "latency"]
    assert len(traces) == len(latencies) >= 4
    # Each trace sits inside its turn: before that turn's turn_done.
    for i, f in enumerate(frames):
        if f["type"] == "turn_trace":
            assert any(g["type"] == "turn_done" for g in frames[i + 1 :])
    assert any(t["affordability"] for t in traces)  # operator stream keeps it
