"""Hand-built scenario results → metrics aggregation + threshold checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.metrics import (
    LADDER_BRANCHES,
    RATE_METRICS,
    aggregate,
    check_thresholds,
    ladder_branches,
    load_thresholds,
    render_summary_md,
    wilson_interval,
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
            should_escalate=True,
            got_deal=False,
            agreement_valid=None,
            escalated=True,
            final_reason="above_accept_line",
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
        "guard_blocks": 0,
        "rule_extraction_accuracy": 0.9,
        "false_known_rate": 0.0,
        "counters_spoken_max": 4,
        "max_counters": 4,
    }
    assert check_thresholds(good) == []

    bad = {
        "unverified_figures_spoken": 2,
        "sensitive_leaks": 0,
        "agreement_valid": 0.5,
        "escalation_correct": 0.5,
        "no_deal_correct": 0.0,
        "deal_rate_given_zopa": 0.0,
        "guard_blocks": 0,
        "rule_extraction_accuracy": 0.1,
        "false_known_rate": 0.9,
    }
    fails = check_thresholds(bad)
    assert any("unverified_figures_spoken" in f for f in fails)
    assert any("agreement_valid" in f for f in fails)
    assert any("escalation_correct" in f for f in fails)
    assert any("no_deal_correct" in f for f in fails)
    assert any("deal_rate_given_zopa" in f for f in fails)
    assert any("rule_extraction_accuracy" in f for f in fails)
    assert any("false_known_rate" in f for f in fails)


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
    assert th["counters_spoken_max"] == "<=max_counters"
    # Vacuous gate (always true) removed; metric is still reported.
    assert "guard_blocks" not in th


def test_wilson_interval_known_values() -> None:
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(10, 10)  # type: ignore[misc]
    assert lo == pytest.approx(0.7225, abs=1e-3)
    assert hi == 1.0
    lo, hi = wilson_interval(5, 10)  # type: ignore[misc]
    assert lo == pytest.approx(0.2366, abs=1e-3)
    assert hi == pytest.approx(0.7634, abs=1e-3)


def test_every_rate_has_n_and_ci_in_json_and_md() -> None:
    summary = aggregate([_ok(scenario_id="d1"), _ok(scenario_id="d2")])
    for name in RATE_METRICS:
        assert f"{name}_n" in summary
        assert f"{name}_ci95" in summary
    md = render_summary_md(summary)
    assert "| metric | value | n | 95% CI |" in md
    assert "| deal_rate_given_zopa | 1 | 2 | 0.342–1.000 |" in md


def test_quality_metrics_aggregate() -> None:
    results = [
        _ok(
            scenario_id="a",
            counters_spoken=2,
            max_counters=4,
            identical_consecutive_agent_moves=1,
            turns_to_outcome=6,
            hit_max_turns=False,
        ),
        _ok(
            scenario_id="b",
            counters_spoken=9,
            max_counters=4,
            identical_consecutive_agent_moves=3,
            turns_to_outcome=30,
            hit_max_turns=True,
        ),
    ]
    s = aggregate(results)
    assert s["counters_spoken_max"] == 9
    assert s["counters_spoken_mean"] == pytest.approx(5.5)
    assert s["max_counters"] == 4
    assert s["identical_consecutive_agent_moves"] == 4
    assert s["stuck_calls"] == 1
    assert s["stuck_rate"] == pytest.approx(0.5)
    assert s["stuck_rate_n"] == 2
    # Stuck calls never reached an outcome; excluded from turns_to_outcome.
    assert s["turns_to_outcome"] == pytest.approx(6.0)


def test_counter_gate_compares_against_max_counters_key() -> None:
    base = {
        "unverified_figures_spoken": 0,
        "sensitive_leaks": 0,
        "agreement_valid": 1.0,
        "escalation_correct": 1.0,
        "no_deal_correct": 1.0,
        "deal_rate_given_zopa": 1.0,
        "rule_extraction_accuracy": 1.0,
        "false_known_rate": 0.0,
        "max_counters": 4,
    }
    assert check_thresholds({**base, "counters_spoken_max": 4}) == []
    fails = check_thresholds({**base, "counters_spoken_max": 5})
    assert fails == ["counters_spoken_max: got 5, want '<=max_counters'"]
    # Missing reference key fails closed.
    no_cap = {k: v for k, v in base.items() if k != "max_counters"}
    assert check_thresholds({**no_cap, "counters_spoken_max": 0})


def test_no_deal_correct_needs_a_fitting_handoff() -> None:
    """Phase 45: a no_fix call is correct only when it hands off for a price reason."""
    base = dict(stratum="no_fix", zopa=False, should_escalate=True, got_deal=False,
                agreement_valid=None, surplus_captured=None, turns_to_proposal=None)
    results = [
        _ok(scenario_id="a", escalated=True, final_reason="above_accept_line", **base),
        _ok(scenario_id="b", escalated=True, final_reason="infeasible", **base),
        # Loop-guard handoff: no deal, but the call went in circles.
        _ok(scenario_id="c", escalated=True, final_reason="max_turns", **base),
        # Old-style no-deal end (not a handoff) no longer counts.
        _ok(scenario_id="d", escalated=False, final_reason="max_counters", **base),
        # Pressuring personas are scored by escalation_correct only.
        _ok(scenario_id="e", persona="pressuring", escalated=True,
            final_reason="sensitive_request", **base),
    ]
    summary = aggregate(results)
    assert summary["no_deal_correct_n"] == 4
    assert summary["no_deal_correct"] == 0.5
    assert summary["escalation_correct_n"] == 5
    assert summary["escalation_correct"] == 0.8


def _decide(intent: str, reason: str) -> dict:
    return {"type": "decide", "payload": {"intent": intent, "reason": reason}}


def test_ladder_branches_from_decide_events() -> None:
    events = [
        _decide("ASK_SETTLEMENT", "ask"),
        _decide("COUNTER", "bp=4800"),
        _decide("COUNTER", "hold"),
        _decide("COUNTER", "step"),
        _decide("COUNTER", "bp=5100"),
        {"type": "accept_terms_alt", "payload": {}},
        _decide("COUNTER", "bp=5300"),
        _decide("COUNTER", "final_counter"),
        _decide("CONFIRM_SCHEDULE", "rep_firm"),
        _decide("CONFIRM_SCHEDULE", "bp=5600"),
        _decide("ESCALATE", "no_progress"),
    ]
    got = ladder_branches(events)
    assert list(got) == list(LADDER_BRANCHES)
    assert {k: v for k, v in got.items() if v} == {
        "anchor": 1,
        "hold": 1,
        "equal_step": 1,
        "concede_half": 1,
        "reanchor_after_term_change": 1,
        "final_counter": 1,
        "accept_on_repeat": 1,
        "our_counter_accepted": 1,
        "loop_guard_no_progress": 1,
    }
    # A confirm with no counter before it is not a ladder move.
    assert not any(ladder_branches([_decide("CONFIRM_SCHEDULE", "bp=5000")]).values())


def test_ladder_and_rewrite_blocks_aggregate_without_thresholds() -> None:
    a = _ok(
        ladder_branches={"anchor": 1, "hold": 2},
        sim_rewrite={"attempts": 4, "fallbacks": {"figures": 1}},
    )
    b = _ok(
        ladder_branches={"anchor": 1},
        sim_rewrite={"attempts": 6, "fallbacks": {"figures": 1, "stance": 1}},
    )
    s = aggregate([a, b])
    assert s["ladder_branches"]["anchor"] == 2 and s["ladder_branches"]["hold"] == 2
    assert s["ladder_branches"]["counters_exhausted"] == 0
    assert s["sim_rewrite_fallbacks"] == {
        "attempts": 10,
        "count": 3,
        "rate": 0.3,
        "by_reason": {"figures": 2, "stance": 1},
    }
    md = render_summary_md(s)
    assert "| hold | 2 |" in md
    assert "3 of 10 LLM rewrites dropped" in md and "figures=2, stance=1" in md
    assert "ladder_branches" not in load_thresholds()
    assert "sim_rewrite_fallbacks" not in load_thresholds()
    # Template phrasing: no attempts, rate is null rather than a vacuous 0.
    assert aggregate([_ok()])["sim_rewrite_fallbacks"]["rate"] is None
