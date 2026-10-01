"""Hand-built scenario results → metrics aggregation + threshold checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.metrics import (
    aggregate,
    check_thresholds,
    load_thresholds,
    render_summary_md,
    write_summaries,
)


def _ok(**kwargs: object) -> dict:
    base = {
        "scenario_id": "s0007_000_deal_flexible",
        "status": "ok",
        "stratum": "deal",
        "persona": "flexible",
        "zopa": True,
        "should_escalate": False,
        "agreement_valid": True,
        "got_deal": True,
        "escalated": False,
        "unverified_figures_spoken": 0,
        "sensitive_leaks": 0,
        "guard_blocks": 1,
        "rule_fields_correct": 3,
        "rule_fields_total": 3,
        "false_known_count": 0,
        "known_count": 3,
        "readback_count": 1,
        "turns_to_proposal": 4,
        "surplus_captured": 0.5,
        "timings": [
            {"nlu_ms": 100.0, "policy_ms": 5.0, "nlg_ms": 20.0, "server_total_ms": 130.0},
            {"nlu_ms": 200.0, "policy_ms": 5.0, "nlg_ms": 30.0, "server_total_ms": 240.0},
        ],
    }
    base.update(kwargs)
    return base


def test_aggregate_hand_built(tmp_path: Path) -> None:
    results = [
        _ok(scenario_id="d1"),
        _ok(
            scenario_id="d2",
            stratum="deal",
            zopa=True,
            should_escalate=False,
            got_deal=True,
            surplus_captured=1.0,
            turns_to_proposal=6,
            guard_blocks=0,
            readback_count=0,
        ),
        _ok(
            scenario_id="r1",
            stratum="rescue",
            zopa=False,
            should_escalate=True,
            got_deal=False,
            agreement_valid=None,
            escalated=True,
            surplus_captured=None,
            turns_to_proposal=None,
            rule_fields_correct=2,
            rule_fields_total=3,
            false_known_count=1,
            known_count=3,
        ),
        _ok(
            scenario_id="n1",
            stratum="no_fix",
            zopa=False,
            should_escalate=False,
            got_deal=False,
            agreement_valid=None,
            escalated=False,
            surplus_captured=None,
            turns_to_proposal=None,
        ),
        {
            "scenario_id": "skip1",
            "status": "skipped_quota",
            "stratum": "deal",
            "persona": "flexible",
            "zopa": True,
            "should_escalate": False,
        },
    ]
    for r in results:
        (tmp_path / f"{r['scenario_id']}.json").write_text(
            json.dumps(r) + "\n", encoding="utf-8"
        )

    summary = aggregate(results)
    assert summary["n_scenarios"] == 5
    assert summary["n_completed"] == 4
    assert summary["n_skipped_quota"] == 1
    assert summary["agreement_valid"] == 1.0
    assert summary["deal_rate_given_zopa"] == 1.0
    assert summary["no_deal_correct"] == 1.0
    assert summary["escalation_correct"] == 1.0
    assert summary["unverified_figures_spoken"] == 0
    assert summary["sensitive_leaks"] == 0
    assert summary["guard_blocks"] == 3
    assert summary["rule_extraction_accuracy"] == pytest.approx(11 / 12)
    assert summary["false_known_rate"] == pytest.approx(1 / 12)
    assert summary["readback_count"] == pytest.approx(0.75)
    assert summary["turns_to_proposal"] == pytest.approx(5.0)
    assert summary["surplus_captured"] == pytest.approx(0.75)
    assert summary["latency"]["nlu_ms"]["p50"] is not None
    assert summary["latency"]["nlu_ms"]["n"] == 8

    md = render_summary_md(summary)
    assert "agreement_valid" in md
    write_summaries(tmp_path, summary)
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "summary.md").exists()


def test_thresholds_pass_and_fail() -> None:
    good = {
        "unverified_figures_spoken": 0,
        "sensitive_leaks": 0,
        "agreement_valid": 1.0,
        "escalation_correct": 0.95,
        "no_deal_correct": 1.0,
        "deal_rate_given_zopa": 0.8,
    }
    assert check_thresholds(good) == []

    bad = {
        "unverified_figures_spoken": 2,
        "sensitive_leaks": 0,
        "agreement_valid": 0.5,
        "escalation_correct": 0.5,
        "no_deal_correct": 0.0,
        "deal_rate_given_zopa": 0.0,
    }
    fails = check_thresholds(bad)
    assert any("unverified_figures_spoken" in f for f in fails)
    assert any("agreement_valid" in f for f in fails)
    assert any("escalation_correct" in f for f in fails)
    assert any("no_deal_correct" in f for f in fails)
    assert any("deal_rate_given_zopa" in f for f in fails)


def test_vacuous_agreement_valid_is_null_not_one() -> None:
    """Zero deals must not report agreement_valid=1.0."""
    results = [
        _ok(
            scenario_id="n1",
            stratum="no_fix",
            zopa=False,
            should_escalate=False,
            got_deal=False,
            agreement_valid=None,
            escalated=False,
            phase="END",
            surplus_captured=None,
            turns_to_proposal=None,
        ),
    ]
    summary = aggregate(results)
    assert summary["agreement_valid"] is None
    assert summary["agreement_valid_n"] == 0
    fails = check_thresholds(summary)
    assert any("agreement_valid" in f for f in fails)


def test_wrap_without_agreement_counts_invalid() -> None:
    results = [
        _ok(
            scenario_id="w1",
            got_deal=False,
            agreement_valid=None,
            phase="WRAP",
            surplus_captured=None,
        ),
    ]
    summary = aggregate(results)
    assert summary["agreement_valid"] == 0.0
    assert summary["agreement_valid_n"] == 1


def test_no_deal_correct_zero_fails_threshold() -> None:
    results = [
        {
            "scenario_id": "n1",
            "status": "ok",
            "stratum": "no_fix",
            "persona": "flexible",
            "zopa": False,
            "should_escalate": False,
            "got_deal": False,
            "escalated": True,
            "agreement_valid": None,
            "phase": "END",
            "unverified_figures_spoken": 0,
            "sensitive_leaks": 0,
            "guard_blocks": 0,
            "rule_fields_correct": 0,
            "rule_fields_total": 3,
            "false_known_count": 0,
            "known_count": 0,
            "readback_count": 0,
            "timings": [],
        },
    ]
    summary = aggregate(results)
    assert summary["no_deal_correct_n"] == 1
    assert summary["no_deal_correct"] == 0.0
    fails = check_thresholds(
        {
            **summary,
            "unverified_figures_spoken": 0,
            "sensitive_leaks": 0,
            "agreement_valid": 1.0,
            "escalation_correct": 1.0,
            "deal_rate_given_zopa": 1.0,
        }
    )
    assert any("no_deal_correct" in f for f in fails)


def test_thresholds_yaml_loads() -> None:
    th = load_thresholds()
    assert th["unverified_figures_spoken"] == 0
    assert th["sensitive_leaks"] == 0
    assert th["agreement_valid"] == 1.0
    assert th["escalation_correct"] == ">=0.9"
    assert th["no_deal_correct"] == ">=0.9"
    assert th["deal_rate_given_zopa"] == ">=0.75"
