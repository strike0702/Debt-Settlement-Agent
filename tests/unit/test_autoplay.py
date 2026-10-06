"""Autoplay: the sim creditor drives a call over the normal WS events, offline."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from app.adapter.validator import validate
from app.autoplay import (
    autoplay_settings,
    load_autoplay_scenario,
    new_autoplay_call,
    run_autoplay,
)
from app.domain.scenario import load_scenario, resolve_scenario_dir
from sim.scenarios import to_creditor_rules
from tests.wsutil import make_client, offline_settings


def _start(ws, scenario_id: str, **extra) -> list[dict]:
    ws.send_json({"type": "start", "scenario_id": scenario_id, "autoplay": True, **extra})
    frames: list[dict] = []
    while not frames or frames[-1]["type"] != "autoplay_done":
        frames.append(ws.receive_json())
    return frames


def test_autoplay_settings_force_oracle_and_no_llm_nlg() -> None:
    s = autoplay_settings(offline_settings(nlu_mode="llm", nlg_mode="llm"))
    assert s.nlu_mode == "oracle" and s.nlg_mode == "template"
    assert autoplay_settings(offline_settings(nlg_mode="bank")).nlg_mode == "bank"


def test_load_autoplay_scenario_reads_sim_truth() -> None:
    call = load_scenario(resolve_scenario_dir("late_start_date"), rebase_to=date(2026, 10, 7))
    sc = load_autoplay_scenario("late_start_date", call)
    assert sc.call is call
    assert sc.true_rules.first_payment_date > call.client.last_draft_date
    with pytest.raises(ValueError):
        load_autoplay_scenario("../etc", call)


async def test_run_autoplay_easy_deal_direct(tmp_path: Path) -> None:
    call = load_scenario(resolve_scenario_dir("easy_deal"), rebase_to=date(2026, 10, 7))
    orch, creditor = new_autoplay_call(call, "easy_deal", settings=offline_settings(), audit=None)
    agent, cred = [], []

    async def on_agent(utt):
        agent.append(utt)

    async def on_creditor(text):
        cred.append(text)

    result = await run_autoplay(orch, creditor, on_agent=on_agent, on_creditor=on_creditor)
    assert result.outcome == "deal" and result.phase == "WRAP"
    assert len(agent) == len(cred) + 1  # opening has no creditor line before it
    agreement = orch.session.agreement
    assert agreement is not None
    # Valid under the creditor's hidden (agreed) rules, not just our belief.
    truth = creditor.agreed_rules
    summary = orch.session.last_eval
    assert summary is not None and summary.rows is not None
    rules = to_creditor_rules(
        truth, program_fee_pct=call.program_fee_pct, bank_fee_cents=call.bank_fee_cents
    )
    assert validate(
        summary.rows, call.client, summary.offer_total_cents, summary.program_fee_cents,
        rules, truth.first_payment_date,
    ) == []  # fmt: skip


def test_autoplay_easy_deal_reaches_wrap_with_valid_agreement(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/ap-1") as ws:
        frames = _start(ws, "easy_deal", autoplay_pause_ms=0)
    done = frames[-1]
    assert done["outcome"] == "deal" and done["phase"] == "WRAP"
    agreements = [f for f in frames if f["type"] == "agreement"]
    assert len(agreements) == 1
    ag = agreements[0]
    assert ag["status"] == "pending_client_approval" and ag["rows"] and ag["bp"] >= 4000
    creditor_lines = [f for f in frames if f["type"] == "transcript" and f["role"] == "creditor"]
    assert creditor_lines
    assert sum(f["type"] == "turn_trace" for f in frames) == len(creditor_lines) + 1
    assert not any(f["type"] == "error" for f in frames)


def test_autoplay_no_space_ends_no_deal(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/ap-2") as ws:
        frames = _start(ws, "no_space", autoplay_pause_ms=0)
    assert frames[-1]["outcome"] == "no_deal" and frames[-1]["phase"] == "END"
    assert not any(f["type"] == "agreement" for f in frames)
    assert any(f["type"] == "phase" and f["intent"] == "NO_DEAL_WRAP" for f in frames)


def test_autoplay_rescue_escalate_escalates(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/ap-3") as ws:
        frames = _start(ws, "rescue_escalate", autoplay_pause_ms=0)
    assert frames[-1]["outcome"] == "escalate" and frames[-1]["phase"] == "ESCALATE"
    assert any(f["type"] == "escalate" and f["reason"] == "out_of_guardrail" for f in frames)


def test_autoplay_makes_no_llm_calls(tmp_path: Path) -> None:
    class _NoCalls:
        on_call = None

        async def chat_text(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("autoplay called the LLM")

        chat_json = transcribe = chat_text

    settings = offline_settings(nlu_mode="llm", nlg_mode="llm", llm_profile="demo")
    with make_client(tmp_path, settings=settings, llm=_NoCalls()) as client:
        with client.websocket_connect("/ws/call/ap-4") as ws:
            frames = _start(ws, "easy_deal", autoplay_pause_ms=0)
    assert frames[-1]["outcome"] == "deal"


def test_autoplay_pause_is_applied_between_turns(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/ap-5") as ws:
        t0 = time.perf_counter()
        frames = _start(ws, "rescue_escalate", autoplay_pause_ms=40)
        elapsed = time.perf_counter() - t0
    turns = sum(f["type"] == "turn_trace" for f in frames)
    assert elapsed >= 0.04 * (turns - 1)


def test_autoplay_rejects_text_and_unknown_scenario(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        with client.websocket_connect("/ws/call/ap-6") as ws:
            ws.send_json({"type": "start", "scenario": "fixtures/demo", "autoplay": True})
            msg = ws.receive_json()
            assert msg["type"] == "error" and "autoplay" in msg["message"]
        with client.websocket_connect("/ws/call/ap-7") as ws:
            ws.send_json(
                {"type": "start", "scenario_id": "easy_deal", "autoplay": True,
                 "autoplay_pause_ms": 200}
            )  # fmt: skip
            ws.receive_json()  # first frame of the opening
            ws.send_json({"type": "text", "text": "hello"})
            seen = []
            while not seen or seen[-1]["type"] != "autoplay_done":
                seen.append(ws.receive_json())
            assert any(f["type"] == "error" and "autoplay" in f["message"] for f in seen)


def test_sim_does_not_import_app_agent() -> None:
    """Autoplay lives in ``app/``; ``sim/`` must stay independent of the agent."""
    import subprocess
    import sys

    code = (
        "import sys, sim.creditor, sim.scenarios, sim.personas; "
        "print(sorted(m for m in sys.modules if m.startswith('app.agent')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]"
