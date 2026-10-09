"""Aggregate per-scenario eval JSON into summary.json / summary.md.

Every metric named in PLAN §10: agreement_valid, deal_rate_given_zopa,
no_deal_correct, escalation_correct, unverified_figures_spoken,
sensitive_leaks, guard_blocks, rule_extraction_accuracy, false_known_rate,
readback_count, turns_to_proposal, surplus_captured, plus latency p50/p95.

Deterministic call-quality metrics: counters_spoken_max / _mean (vs
max_counters), identical_consecutive_agent_moves, turns_to_outcome,
stuck_calls / stuck_rate (hit max_turns).

Phase 45 (deal-or-handoff): a call ends only in a deal or a handoff, so
``no_deal_correct`` now scores no_fix calls (non-pressuring) as correct when
they end in a handoff with a price / feasibility reason
(``NO_DEAL_HANDOFF_REASONS``) and no deal. ``escalation_correct`` counts every
``should_escalate`` call, which now includes no_fix and above-the-line floors.

Every rate ships with ``<rate>_n`` and a 95% Wilson interval ``<rate>_ci95``.
Rule-field rates pool 7 fields per call, so their interval is optimistic
(fields within a call are correlated). Does not run scenarios — that is
``eval.run_eval``.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

import yaml

# Handoff reasons that fit a call with no possible deal (Phase 45): no plan fits,
# nothing to offer below the ask, or the rep stayed above the accept line or
# rejected the schedule. Loop-guard reasons (max_turns, no_progress,
# repeated_question) are not "appropriate": they mean the call went in circles.
NO_DEAL_HANDOFF_REASONS: frozenset[str] = frozenset(
    {
        "infeasible",
        "no_legal_counter",
        "max_counters",
        "above_accept_line",
        "confirm_rejected",
        "confirm_unacked",
    }
)

# Rates reported with n + Wilson CI, in summary.md row order.
RATE_METRICS: tuple[str, ...] = (
    "agreement_valid",
    "deal_rate_given_zopa",
    "no_deal_correct",
    "escalation_correct",
    "rule_extraction_accuracy",
    "false_known_rate",
    "stuck_rate",
)


def _mean(xs: list[float]) -> float | None:
    return statistics.mean(xs) if xs else None


def _rate(num: int, den: int) -> float | None:
    if den == 0:
        return None
    return num / den


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for ``k`` successes in ``n`` trials (None if n=0)."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate_block(name: str, k: int, n: int) -> dict[str, Any]:
    return {
        name: _rate(k, n),
        f"{name}_n": n,
        f"{name}_ci95": wilson_interval(k, n),
    }


def _percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ys[int(k)]
    return ys[f] * (c - k) + ys[c] * (k - f)


def load_scenario_results(run_dir: Path) -> list[dict[str, Any]]:
    """Load every ``*.json`` in ``run_dir`` except run/summary metadata."""
    skip = {"run.json", "summary.json"}
    out: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*.json")):
        if path.name in skip:
            continue
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute PLAN §10 metrics over completed (non-skipped) scenarios."""
    completed = [r for r in results if r.get("status") == "ok"]
    skipped = [r for r in results if r.get("status") == "skipped_quota"]
    errors = [r for r in results if r.get("status") == "error"]

    # --- agreement_valid ---
    # Score drafted agreements and WRAP-sans-agreement. Empty denom → null
    # (never vacuous 1.0). WRAP without a deal is always invalid.
    agr_ok = 0
    agr_n = 0
    for r in completed:
        v = r.get("agreement_valid")
        phase = r.get("phase")
        if phase == "WRAP" and not r.get("got_deal"):
            agr_n += 1
            continue
        if v is None:
            continue
        agr_n += 1
        if v:
            agr_ok += 1

    # --- deal_rate_given_zopa ---
    zopa_deal_ok = 0
    zopa_n = 0
    for r in completed:
        if not r.get("zopa"):
            continue
        if r.get("should_escalate"):
            continue
        zopa_n += 1
        if r.get("got_deal"):
            zopa_deal_ok += 1

    # --- no_deal_correct (Phase 45: no deal, handed off for a fitting reason) ---
    no_deal_ok = 0
    no_deal_n = 0
    for r in completed:
        if r.get("stratum") != "no_fix" or r.get("persona") == "pressuring":
            continue
        no_deal_n += 1
        if (
            not r.get("got_deal")
            and r.get("escalated")
            and r.get("final_reason") in NO_DEAL_HANDOFF_REASONS
        ):
            no_deal_ok += 1

    # --- escalation_correct ---
    esc_ok = 0
    esc_n = 0
    for r in completed:
        if not r.get("should_escalate"):
            continue
        esc_n += 1
        if r.get("escalated"):
            esc_ok += 1

    unverified = sum(int(r.get("unverified_figures_spoken", 0)) for r in completed)
    leaks = sum(int(r.get("sensitive_leaks", 0)) for r in completed)
    blocks = sum(int(r.get("guard_blocks", 0)) for r in completed)

    # --- rule_extraction_accuracy / false_known_rate ---
    rule_correct = 0
    rule_total = 0
    false_known = 0
    known_total = 0
    for r in completed:
        rule_correct += int(r.get("rule_fields_correct", 0))
        rule_total += int(r.get("rule_fields_total", 0))
        false_known += int(r.get("false_known_count", 0))
        known_total += int(r.get("known_count", 0))

    readbacks = [float(r.get("readback_count", 0)) for r in completed]
    turns = [
        float(r["turns_to_proposal"])
        for r in completed
        if r.get("turns_to_proposal") is not None
    ]
    surplus = [
        float(r["surplus_captured"])
        for r in completed
        if r.get("surplus_captured") is not None
    ]

    # --- call quality ---
    counters = [int(r.get("counters_spoken", 0)) for r in completed]
    identical = sum(int(r.get("identical_consecutive_agent_moves", 0)) for r in completed)
    stuck = sum(1 for r in completed if r.get("hit_max_turns"))
    outcome_turns = [
        float(r["turns_to_outcome"])
        for r in completed
        if r.get("turns_to_outcome") is not None and not r.get("hit_max_turns")
    ]
    caps = [int(r["max_counters"]) for r in completed if r.get("max_counters") is not None]

    # --- latency ---
    stages = ("nlu_ms", "policy_ms", "nlg_ms", "server_total_ms")
    latency: dict[str, dict[str, float | None]] = {}
    for stage in stages:
        vals: list[float] = []
        for r in completed:
            for t in r.get("timings") or []:
                if stage in t and t[stage] is not None:
                    vals.append(float(t[stage]))
        latency[stage] = {
            "p50": _percentile(vals, 0.50),
            "p95": _percentile(vals, 0.95),
            "n": len(vals),
        }

    return {
        "n_scenarios": len(results),
        "n_completed": len(completed),
        "n_skipped_quota": len(skipped),
        "n_error": len(errors),
        # null when empty denom — thresholds fail closed (never vacuous 1.0).
        **_rate_block("agreement_valid", agr_ok, agr_n),
        **_rate_block("deal_rate_given_zopa", zopa_deal_ok, zopa_n),
        **_rate_block("no_deal_correct", no_deal_ok, no_deal_n),
        **_rate_block("escalation_correct", esc_ok, esc_n),
        "unverified_figures_spoken": unverified,
        "sensitive_leaks": leaks,
        "guard_blocks": blocks,
        **_rate_block("rule_extraction_accuracy", rule_correct, rule_total),
        **_rate_block("false_known_rate", false_known, known_total),
        "readback_count": _mean(readbacks),
        "turns_to_proposal": _mean(turns),
        "surplus_captured": _mean(surplus),
        "surplus_n": len(surplus),
        "counters_spoken_max": max(counters) if counters else None,
        "counters_spoken_mean": _mean([float(c) for c in counters]),
        "max_counters": max(caps) if caps else None,
        "identical_consecutive_agent_moves": identical,
        "turns_to_outcome": _mean(outcome_turns),
        "stuck_calls": stuck,
        **_rate_block("stuck_rate", stuck, len(completed)),
        "latency": latency,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if v == int(v) and abs(v) >= 1:
            return str(int(v))
        return f"{v:.3f}"
    return str(v)


def render_summary_md(summary: dict[str, Any], *, run_meta: dict[str, Any] | None = None) -> str:
    """Markdown table of metrics for PROGRESS / README paste."""
    lines: list[str] = ["# Eval summary", ""]
    if run_meta:
        lines.append(
            f"- run_id: `{run_meta.get('run_id', '')}`  "
            f"seed={run_meta.get('seed')}  profile=`{run_meta.get('profile')}`  "
            f"nlu=`{run_meta.get('nlu')}`  nlg=`{run_meta.get('nlg')}`  "
            f"sim=`{run_meta.get('sim_phrasing')}`  "
            f"oracle_overlay=`{run_meta.get('oracle_overlay')}`"
        )
        lines.append(f"- git: `{run_meta.get('git_sha', '')}`")
        share = run_meta.get("call_share") or {}
        if share:
            parts = ", ".join(f"{k}={v:.1%}" for k, v in sorted(share.items()))
            lines.append(f"- model share: {parts}")
        lines.append("")

    lines += ["| metric | value | n | 95% CI |", "|---|---|---|---|"]
    lines.append(
        f"| n_completed / n_scenarios | {summary['n_completed']}/{summary['n_scenarios']} | | |"
    )
    lines.append(f"| skipped_quota | {summary['n_skipped_quota']} | | |")
    for name in RATE_METRICS:
        ci = summary.get(f"{name}_ci95")
        ci_s = f"{ci[0]:.3f}–{ci[1]:.3f}" if ci else "n/a"
        lines.append(
            f"| {name} | {_fmt(summary.get(name))} | {summary.get(f'{name}_n', 0)} | {ci_s} |"
        )
    plain = [
        ("unverified_figures_spoken", summary["unverified_figures_spoken"]),
        ("sensitive_leaks", summary["sensitive_leaks"]),
        ("guard_blocks", summary["guard_blocks"]),
        ("counters_spoken_max", summary.get("counters_spoken_max")),
        ("max_counters", summary.get("max_counters")),
        ("counters_spoken (mean)", summary.get("counters_spoken_mean")),
        ("identical_consecutive_agent_moves", summary.get("identical_consecutive_agent_moves")),
        ("stuck_calls", summary.get("stuck_calls")),
        ("turns_to_outcome (mean)", summary.get("turns_to_outcome")),
        ("readback_count (mean)", summary["readback_count"]),
        ("turns_to_proposal (mean)", summary["turns_to_proposal"]),
        ("surplus_captured (mean)", summary["surplus_captured"]),
    ]
    for name, val in plain:
        lines.append(f"| {name} | {_fmt(val)} | | |")

    lines += ["", "## Latency (ms)", "", "| stage | p50 | p95 | n |", "|---|---|---|---|"]
    for stage, stats in (summary.get("latency") or {}).items():
        lines.append(
            f"| {stage} | {_fmt(stats.get('p50'))} | {_fmt(stats.get('p95'))} | "
            f"{stats.get('n', 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_summaries(
    run_dir: Path,
    summary: dict[str, Any],
    *,
    run_meta: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Write ``summary.json`` and ``summary.md`` under ``run_dir``."""
    json_path = run_dir / "summary.json"
    md_path = run_dir / "summary.md"
    json_path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_summary_md(summary, run_meta=run_meta), encoding="utf-8")
    return json_path, md_path


def load_thresholds(path: Path | None = None) -> dict[str, Any]:
    """Load ``eval/thresholds.yaml``."""
    p = path or Path(__file__).resolve().parent / "thresholds.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"bad thresholds file: {p}")
    return data


def _operand(raw: str, summary: dict[str, Any]) -> float | None:
    """Threshold RHS: a number, or the name of another summary key (e.g. ``max_counters``)."""
    s = raw.strip()
    try:
        return float(s)
    except ValueError:
        v = summary.get(s)
        return None if v is None else float(v)


def _compare(actual: Any, expect: Any, summary: dict[str, Any]) -> bool:
    """Return True if ``actual`` meets the threshold ``expect``."""
    if actual is None:
        return False
    if isinstance(expect, str):
        s = expect.strip()
        # Two-char operators first so ">=" is not read as ">".
        for op in (">=", "<=", ">", "<"):
            if s.startswith(op):
                rhs = _operand(s[len(op) :], summary)
                if rhs is None:
                    return False
                a = float(actual)
                return {
                    ">=": a >= rhs,
                    "<=": a <= rhs,
                    ">": a > rhs,
                    "<": a < rhs,
                }[op]
        rhs = _operand(s, summary)
        return rhs is not None and float(actual) == rhs
    if isinstance(expect, (int, float)):
        return float(actual) == float(expect) or (
            isinstance(expect, float)
            and abs(float(actual) - float(expect)) < 1e-9
        )
    return actual == expect


def check_thresholds(
    summary: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
) -> list[str]:
    """Return list of failure messages (empty ⇒ pass)."""
    th = thresholds if thresholds is not None else load_thresholds()
    failures: list[str] = []
    for key, expect in th.items():
        actual = summary.get(key)
        if not _compare(actual, expect, summary):
            failures.append(f"{key}: got {actual!r}, want {expect!r}")
    return failures
