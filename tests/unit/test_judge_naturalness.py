"""Blind pairwise naturalness judge (Phase 24a): order swap, Wilson CI, human CSV."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.llm.client import FakeLLM
from eval.judge_naturalness import judge_pair, run_judge, summarize


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
