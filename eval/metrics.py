"""Aggregate per-scenario eval JSON into summary.json / summary.md.

Every metric named in PLAN §10: agreement_valid, deal_rate_given_zopa,
no_deal_correct, escalation_correct, unverified_figures_spoken,
sensitive_leaks, guard_blocks, rule_extraction_accuracy, false_known_rate,
readback_count, turns_to_proposal, surplus_captured, plus latency p50/p95.
Does not run scenarios — that is ``eval.run_eval``.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

import yaml


def _mean(xs: list[float]) -> float | None:
    return statistics.mean(xs) if xs else None


def _rate(num: int, den: int) -> float | None:
    if den == 0:
        return None
    return num / den


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
    agr_ok = 0
    agr_n = 0
    for r in completed:
        v = r.get("agreement_valid")
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

    # --- no_deal_correct ---
    no_deal_ok = 0
    no_deal_n = 0
    for r in completed:
        if r.get("stratum") != "no_fix":
            continue
        if r.get("should_escalate"):
            continue
        no_deal_n += 1
        if not r.get("got_deal") and not r.get("escalated"):
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
        # Vacuous 1.0 when the denominator is empty (threshold keys).
        "agreement_valid": _rate(agr_ok, agr_n) if agr_n else 1.0,
        "agreement_valid_n": agr_n,
        "deal_rate_given_zopa": _rate(zopa_deal_ok, zopa_n),
        "deal_rate_given_zopa_n": zopa_n,
        "no_deal_correct": _rate(no_deal_ok, no_deal_n),
        "no_deal_correct_n": no_deal_n,
        "escalation_correct": _rate(esc_ok, esc_n) if esc_n else 1.0,
        "escalation_correct_n": esc_n,
        "unverified_figures_spoken": unverified,
        "sensitive_leaks": leaks,
        "guard_blocks": blocks,
        "rule_extraction_accuracy": _rate(rule_correct, rule_total),
        "rule_extraction_n": rule_total,
        "false_known_rate": _rate(false_known, known_total),
        "false_known_n": known_total,
        "readback_count": _mean(readbacks),
        "turns_to_proposal": _mean(turns),
        "surplus_captured": _mean(surplus),
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
            f"nlg=`{run_meta.get('nlg')}`  sim=`{run_meta.get('sim_phrasing')}`"
        )
        lines.append(f"- git: `{run_meta.get('git_sha', '')}`")
        share = run_meta.get("call_share") or {}
        if share:
            parts = ", ".join(f"{k}={v:.1%}" for k, v in sorted(share.items()))
            lines.append(f"- model share: {parts}")
        lines.append("")

    rows = [
        ("n_completed / n_scenarios", f"{summary['n_completed']}/{summary['n_scenarios']}"),
        ("skipped_quota", summary["n_skipped_quota"]),
        ("agreement_valid", summary["agreement_valid"]),
        ("deal_rate_given_zopa", summary["deal_rate_given_zopa"]),
        ("no_deal_correct", summary["no_deal_correct"]),
        ("escalation_correct", summary["escalation_correct"]),
        ("unverified_figures_spoken", summary["unverified_figures_spoken"]),
        ("sensitive_leaks", summary["sensitive_leaks"]),
        ("guard_blocks", summary["guard_blocks"]),
        ("rule_extraction_accuracy", summary["rule_extraction_accuracy"]),
        ("false_known_rate", summary["false_known_rate"]),
        ("readback_count (mean)", summary["readback_count"]),
        ("turns_to_proposal (mean)", summary["turns_to_proposal"]),
        ("surplus_captured (mean)", summary["surplus_captured"]),
    ]
    lines += ["| metric | value |", "|---|---|"]
    for name, val in rows:
        lines.append(f"| {name} | {_fmt(val)} |")

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


def _compare(actual: Any, expect: Any) -> bool:
    """Return True if ``actual`` meets the threshold ``expect``."""
    if actual is None:
        return False
    if isinstance(expect, str):
        s = expect.strip()
        if s.startswith(">="):
            return float(actual) >= float(s[2:].strip())
        if s.startswith(">"):
            return float(actual) > float(s[1:].strip())
        if s.startswith("<="):
            return float(actual) <= float(s[2:].strip())
        if s.startswith("<"):
            return float(actual) < float(s[1:].strip())
        return float(actual) == float(s)
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
        if not _compare(actual, expect):
            failures.append(f"{key}: got {actual!r}, want {expect!r}")
    return failures
