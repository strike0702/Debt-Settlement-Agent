"""``eval.ab_report``: common-scenario table and the pre-registered adoption rule."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.ab_report import adoption_checks, build_report, common_ids, pick_transcripts


def _result(sid: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scenario_id": sid,
        "status": "ok",
        "stratum": "deal",
        "persona": "flexible",
        "zopa": True,
        "should_escalate": False,
        "phase": "WRAP",
        "got_deal": True,
        "escalated": False,
        "agreement_valid": True,
        "sensitive_leaks": 0,
        "unverified_figures_spoken": 0,
        "surplus_captured": 0.5,
        "turns_to_outcome": 4,
        "final_intent": "PROPOSE_WRAP",
        "final_reason": "confirmed",
        "llm_calls_per_turn": [0, 1, 1],
        "turn_latency_ms": [1.0, 1000.0, 1200.0],
        "transcript": [{"role": "agent", "text": "Hello."}, {"role": "creditor", "text": "Hi."}],
        "intents": ["OPENING", "COUNTER", "PROPOSE_WRAP"],
    }
    base.update(kw)
    return base


def _write(run: Path, results: list[dict[str, Any]], agent: str) -> Path:
    run.mkdir(parents=True)
    for r in results:
        (run / f"{r['scenario_id']}.json").write_text(json.dumps(r), encoding="utf-8")
    (run / "run.json").write_text(json.dumps({"agent": agent, "nlg": "template"}), "utf-8")
    return run


def test_only_scenarios_every_arm_completed_are_compared(tmp_path: Path) -> None:
    a = [_result("s1"), _result("s2"), _result("s3")]
    b = [_result("s1"), _result("s2", status="skipped_quota"), _result("s3")]
    assert common_ids({"A": a, "B": b}) == ["s1", "s3"]


def test_adoption_rule_each_clause_fails_closed() -> None:
    base = {
        "server_total_ms_p50": 1000.0,
        **{f"{k}_ci95": (0.5, 0.9) for k in ("deal_rate_given_zopa", "no_deal_correct")},
        "escalation_correct_ci95": None,  # empty denominator in A → cannot be "within"
    }
    row = {
        "sensitive_leaks": 0,
        "unverified_figures_spoken": 0,
        "agreement_valid": 1.0,
        "deal_rate_given_zopa": 0.7,
        "no_deal_correct": 0.95,
        "escalation_correct": None,
        "server_total_ms_p50": 1499.0,
    }
    c = adoption_checks(row, base, 0.61)
    assert c["zero_leaks_and_unverified"] and c["agreement_valid_is_1"]
    assert c["deal_rate_given_zopa_within_A_ci"]
    assert not c["no_deal_correct_within_A_ci"]
    assert not c["escalation_correct_within_A_ci"]
    assert c["p50_within_A_plus_0_5s"] and c["naturalness_win_rate_ge_60"]
    assert not adoption_checks({**row, "server_total_ms_p50": 1501.0}, base, 0.61)[
        "p50_within_A_plus_0_5s"
    ]
    assert not adoption_checks(row, base, None)["naturalness_win_rate_ge_60"]
    assert not adoption_checks({**row, "unverified_figures_spoken": 1}, base, 0.9)[
        "zero_leaks_and_unverified"
    ]


def test_worst_transcript_prefers_safety_failures() -> None:
    rs = [
        _result("s1"),
        _result("s2", turns_to_outcome=9),
        _result("s3", unverified_figures_spoken=1),
        _result("s4"),
    ]
    rep, worst = pick_transcripts(rs)
    assert worst["scenario_id"] == "s3"
    assert rep["scenario_id"] != "s3"


def test_build_report_table_and_rule(tmp_path: Path) -> None:
    a = _write(tmp_path / "A", [_result("s1"), _result("s2")], "policy")
    b = _write(tmp_path / "B", [_result("s1"), _result("s2", agreement_valid=False)], "policy_h3")
    judge = tmp_path / "jBA"
    judge.mkdir()
    (judge / "summary.json").write_text(
        json.dumps(
            {"a_win_rate": 0.7, "a_win_rate_ci95": [0.4, 0.9], "a_wins": 7, "b_wins": 3, "ties": 0}
        ),
        encoding="utf-8",
    )
    md, data = build_report({"A": a, "B": b}, {"B:A": judge}, decision="Keep A.")
    assert "| A | 2 |" in md and "| B | 2 |" in md
    assert "0.70 [0.40, 0.90] (7–3, 0 ties)" in md
    assert data["adoption"]["B"]["checks"]["agreement_valid_is_1"] is False
    assert data["adoption"]["B"]["adopt"] is False
    assert "## Decision" in md and "Keep A." in md
    assert md.count(": representative") == 2 and md.count(": worst") == 2
