"""Blind pairwise naturalness judge (Phase 24a): order swap, Wilson CI, human CSV."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import pytest

import eval.judge_naturalness as judge_mod
from app.config import Settings
from app.llm.budget import ModelPrice
from app.llm.client import FakeLLM, LLMClient
from eval.judge_naturalness import JudgeCallLog, format_cost, judge_pair, run_judge, summarize

_SONNET = ModelPrice(2_000_000, 10_000_000)  # $2 / $10 per MTok, in micro-dollars


def _write_run(root: Path, name: str, agent: str, ids: list[str]) -> Path:
    run = root / name
    run.mkdir()
    for sid in ids:
        result: dict[str, Any] = {
            "scenario_id": sid,
            "status": "ok",
            "agent": agent,
            "transcript": [
                {"role": "agent", "text": f"{agent} hello"},
                {"role": "creditor", "text": "Hi."},
            ],
        }
        (run / f"{sid}.json").write_text(json.dumps(result), encoding="utf-8")
    return run


async def test_judge_pair_needs_both_orders_to_agree() -> None:
    llm = FakeLLM()
    # A first → "1" (A); B first → "2" (A): consistent A win.
    llm.enqueue("judge", '{"winner": "1"}')
    llm.enqueue("judge", '{"winner": "2"}')
    assert (await judge_pair(llm, "a", "b"))["verdict"] == "A"
    # Always "1": position bias → tie.
    llm.enqueue("judge", '{"winner": "1"}')
    llm.enqueue("judge", '{"winner": "1"}')
    assert (await judge_pair(llm, "a", "b"))["verdict"] == "tie"
    # Garbage counts as tie for that order.
    llm.enqueue("judge", "no idea")
    llm.enqueue("judge", '{"winner": "1"}')
    res = await judge_pair(llm, "a", "b")
    assert (res["a_first"], res["b_first"], res["verdict"]) == ("tie", "B", "tie")


def test_summarize_wilson_over_decisive_pairs() -> None:
    out = summarize(["A", "A", "A", "B", "tie"])
    assert (out["a_wins"], out["b_wins"], out["ties"]) == (3, 1, 1)
    assert out["a_win_rate"] == 0.75
    lo, hi = out["a_win_rate_ci95"]
    assert 0.0 < lo < 0.75 < hi <= 1.0
    assert out["a_win_rate_ties_half"] == 0.7
    assert summarize([])["a_win_rate"] is None


async def test_run_judge_writes_outputs_and_20_human_pairs(tmp_path: Path) -> None:
    ids = [f"s{i:03d}" for i in range(25)]
    run_a = _write_run(tmp_path, "run_a", "policy", ids)
    run_b = _write_run(tmp_path, "run_b", "react", ids[:22] + ["only_b"])
    llm = FakeLLM()
    for _ in range(22):
        llm.enqueue("judge", '{"winner": "1"}')
        llm.enqueue("judge", '{"winner": "2"}')
    out = tmp_path / "judge"
    summary = await run_judge(run_a, run_b, llm=llm, out_dir=out, seed=3)
    assert summary["n_pairs"] == 22
    assert summary["a_wins"] == 22
    assert summary["gate"] is False
    assert (summary["agent_a"], summary["agent_b"]) == ("policy", "react")
    assert all(c["role"] == "judge" for c in llm.calls)
    rows = list(csv.DictReader((out / "human_pairs.csv").open(encoding="utf-8")))
    keys = list(csv.DictReader((out / "human_pairs_key.csv").open(encoding="utf-8")))
    assert len(rows) == 20 and len(keys) == 20
    # Blind: arm names never appear in the rating sheet's headers.
    assert "run_a" not in (out / "human_pairs.csv").read_text(encoding="utf-8")
    assert {k["transcript_1_arm"] for k in keys} <= {"run_a", "run_b"}
    assert json.loads((out / "judge.json").read_text(encoding="utf-8"))[0]["verdict"] == "A"


# --- Phase 47 [24b.8]: judge call log and cost ------------------------------


def _meta(**kw: Any) -> dict[str, Any]:
    base = {
        "role": "judge",
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "latency_ms": 900.0,
        "prompt_tokens": 1500,
        "completion_tokens": 120,
        "cache_hit": False,
        "failover_from": None,
        "error": None,
    }
    return {**base, **kw}


def test_judge_call_log_costs_in_integer_micros(tmp_path: Path) -> None:
    log = JudgeCallLog(
        tmp_path / "judge_calls.jsonl",
        lambda p, m: _SONNET if (p, m) == ("anthropic", "claude-sonnet-5-5") else None,
    )
    log(_meta())  # 1500 × 2 + 120 × 10 = 4200 micro-dollars
    log(_meta(prompt_tokens=333, completion_tokens=7))  # 666 + 70 = 736
    log(_meta(cache_hit=True))  # not billed
    log(_meta(provider="other", model="x"))  # no price: counted, not guessed
    rows = [json.loads(x) for x in (tmp_path / "judge_calls.jsonl").read_text().splitlines()]
    assert [r["cost_usd"] for r in rows] == ["0.0042", "0.000736", "0", None]
    assert rows[0]["model"] == "claude-sonnet-5-5" and rows[0]["latency_ms"] == 900.0
    assert log.totals() == {
        "calls": 4,
        "input_tokens": 1500 + 333 + 1500,
        "output_tokens": 120 + 7 + 120,
        "cost_usd": "0.004936",
        "unpriced_calls": 1,
    }
    assert format_cost(log.totals()) == (
        "judge cost: $0.004936 over 4 calls (3333 in / 247 out); "
        "1 calls on an unpriced model not counted"
    )


async def test_run_judge_records_every_call_and_cost_in_summary(tmp_path: Path) -> None:
    ids = ["s1", "s2"]
    run_a = _write_run(tmp_path, "run_a", "policy", ids)
    run_b = _write_run(tmp_path, "run_b", "react", ids)
    llm = FakeLLM()
    for _ in ids:
        llm.enqueue("judge", '{"winner": "1"}')
        llm.enqueue("judge", '{"winner": "2"}')
    out = tmp_path / "judge"
    log = JudgeCallLog(out / "judge_calls.jsonl", lambda p, m: _SONNET)
    llm.on_call = log
    summary = await run_judge(run_a, run_b, llm=llm, out_dir=out, call_log=log)
    rows = (out / "judge_calls.jsonl").read_text().splitlines()
    assert len(rows) == 4  # two orders per pair
    assert summary["cost"]["calls"] == 4
    assert json.loads((out / "summary.json").read_text())["cost"] == summary["cost"]


async def test_cli_prints_the_cost_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_a = _write_run(tmp_path, "run_a", "policy", ["s1"])
    run_b = _write_run(tmp_path, "run_b", "react", ["s1"])
    llm = FakeLLM()
    llm.enqueue("judge", '{"winner": "1"}')
    llm.enqueue("judge", '{"winner": "2"}')
    monkeypatch.setattr(judge_mod, "make_client", lambda *a, **k: llm)
    args = argparse.Namespace(
        run_a=str(run_a), run_b=str(run_b), profile="offline", limit=None, seed=0,
        human_pairs=1, out=str(tmp_path / "out"),
    )
    assert await judge_mod._async_main(args) == 0
    assert "judge cost: $0 over 2 calls (0 in / 0 out)" in capsys.readouterr().out
    assert len((tmp_path / "out" / "judge_calls.jsonl").read_text().splitlines()) == 2


def test_shipped_providers_price_the_judge_model() -> None:
    client = LLMClient(Settings(llm_profile="eval", api_key_pool={}), skip_health_check=True)
    assert client.price_for("anthropic", "claude-sonnet-5-5") == _SONNET
    assert client.price_for("anthropic", "no-such-model") is None
    assert client.price_for("no-such-provider", "m") is None
