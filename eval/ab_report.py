"""A/B report: one table over several ``eval.run_eval`` arm runs, plus the adoption rule.

Phase 24b (REVIEW_PLAN §2(c)). Reads finished run dirs (one per arm) and
optional ``eval.judge_naturalness`` output dirs, keeps only the scenarios that
**every** arm completed (``status=ok``) so the arms are compared on identical
seeds and scenarios, and writes ``summary.md`` / ``summary.json``:

- one table, rows = arms; columns = leaks, unverified figures, agreement_valid,
  deal / no-deal / escalation rates (n, Wilson CI), surplus, turns, LLM calls
  per turn, server_total p50 / p95, naturalness win-rate vs arm A;
- the pre-registered adoption rule checked for every arm against arm A;
- two transcripts per arm (one representative, one worst).

It does not run calls or the judge, and it is not a CI gate. The decision
paragraph is hand-written and passed in with ``--decision``.

CLI: ``python -m eval.ab_report --arm A=eval/results/<run> --arm B=... \
--judge B:A=eval/results/<judge_dir> --out docs/eval/ab_<date> [--decision FILE]``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from eval.agents.arm_metrics import arm_metrics
from eval.metrics import aggregate, load_scenario_results

OUTCOME_RATES: tuple[str, ...] = ("deal_rate_given_zopa", "no_deal_correct", "escalation_correct")
P50_MARGIN_MS = 500.0
MIN_WIN_RATE = 0.60
_TRANSCRIPT_LINES = 40


def _ok(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["scenario_id"]: r for r in results if r.get("status") == "ok"}


def common_ids(arms: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Scenario ids every arm completed, in sorted order."""
    sets = [set(_ok(rs)) for rs in arms.values()]
    return sorted(set.intersection(*sets)) if sets else []


def outcome(r: dict[str, Any]) -> str:
    """``deal`` | ``escalate`` | ``no_deal``."""
    if r.get("escalated"):
        return "escalate"
    if r.get("got_deal"):
        return "deal"
    return "no_deal"


def _correct(r: dict[str, Any]) -> bool:
    if r.get("should_escalate"):
        return bool(r.get("escalated"))
    if r.get("zopa"):
        return bool(r.get("got_deal")) and r.get("agreement_valid") is not False
    return not r.get("got_deal") and not r.get("escalated")


def badness(r: dict[str, Any]) -> tuple[int, int, int, int]:
    """Sort key for the worst call: safety first, then validity, outcome, length."""
    invalid = 1 if r.get("got_deal") and not r.get("agreement_valid") else 0
    return (
        int(r.get("sensitive_leaks") or 0) + int(r.get("unverified_figures_spoken") or 0),
        invalid,
        0 if _correct(r) else 1,
        int(r.get("turns_to_outcome") or 0),
    )


def pick_transcripts(results: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(representative, worst): the median-length correct call of the modal outcome; max badness."""
    outcomes = [outcome(r) for r in results]
    modal = max(set(outcomes), key=outcomes.count)
    pool = [r for r in results if outcome(r) == modal and _correct(r)] or results
    pool = sorted(pool, key=lambda r: (int(r.get("turns_to_outcome") or 0), r["scenario_id"]))
    rep = pool[len(pool) // 2]
    worst = max(results, key=lambda r: (badness(r), r["scenario_id"]))
    if worst["scenario_id"] == rep["scenario_id"] and len(results) > 1:
        rest = [r for r in results if r["scenario_id"] != rep["scenario_id"]]
        worst = max(rest, key=lambda r: (badness(r), r["scenario_id"]))
    return rep, worst


def arm_row(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Every table column for one arm (``results`` = its common-scenario dicts)."""
    summary = aggregate(results)
    cost = arm_metrics(results)
    turns = [int(r.get("turns_to_outcome") or 0) for r in results]
    return {
        "n": len(results),
        "sensitive_leaks": summary["sensitive_leaks"],
        "unverified_figures_spoken": summary["unverified_figures_spoken"],
        "agreement_valid": summary["agreement_valid"],
        "agreement_valid_n": summary["agreement_valid_n"],
        **{
            f"{k}{suffix}": summary.get(f"{k}{suffix}")
            for k in OUTCOME_RATES
            for suffix in ("", "_n", "_ci95")
        },
        "surplus_captured": summary["surplus_captured"],
        "surplus_n": summary["surplus_n"],
        "turns_mean": statistics.fmean(turns) if turns else None,
        "llm_calls_per_turn_mean": cost["llm_calls_per_turn_mean"],
        "server_total_ms_p50": cost["turn_latency_ms_p50"],
        "server_total_ms_p95": cost["turn_latency_ms_p95"],
    }


def adoption_checks(
    row: dict[str, Any], base: dict[str, Any], win_rate: float | None
) -> dict[str, bool]:
    """REVIEW_PLAN §2(c) rule for one arm vs arm A; a missing input fails closed."""
    checks: dict[str, bool] = {
        "zero_leaks_and_unverified": row["sensitive_leaks"] == 0
        and row["unverified_figures_spoken"] == 0,
        "agreement_valid_is_1": row["agreement_valid"] == 1.0,
    }
    for k in OUTCOME_RATES:
        ci = base.get(f"{k}_ci95")
        v = row.get(k)
        checks[f"{k}_within_A_ci"] = (
            ci is not None and v is not None and ci[0] - 1e-9 <= v <= ci[1] + 1e-9
        )
    p50, base_p50 = row["server_total_ms_p50"], base["server_total_ms_p50"]
    checks["p50_within_A_plus_0_5s"] = (
        p50 is not None and base_p50 is not None and p50 <= base_p50 + P50_MARGIN_MS
    )
    checks["naturalness_win_rate_ge_60"] = win_rate is not None and win_rate >= MIN_WIN_RATE
    return checks


def load_judge(path: Path) -> dict[str, Any]:
    """``summary.json`` of an ``eval.judge_naturalness`` run (RUN_A = the arm being rated)."""
    return json.loads((path / "summary.json").read_text(encoding="utf-8"))


def _fmt(v: Any, digits: int = 3) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _rate(row: dict[str, Any], k: str) -> str:
    ci = row.get(f"{k}_ci95")
    ci_s = f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else ""
    return f"{_fmt(row.get(k), 2)} (n={row.get(f'{k}_n') or 0}){ci_s}"


def _win(j: dict[str, Any] | None) -> str:
    if not j:
        return "–"
    ci = j.get("a_win_rate_ci95")
    ci_s = f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else ""
    return (
        f"{_fmt(j.get('a_win_rate'), 2)}{ci_s} "
        f"({j.get('a_wins')}–{j.get('b_wins')}, {j.get('ties')} ties)"
    )


def _transcript_md(r: dict[str, Any]) -> str:
    lines = [f"{'Agent' if t['role'] == 'agent' else 'Rep'}: {t['text']}" for t in r["transcript"]]
    more = len(lines) - _TRANSCRIPT_LINES
    body = "\n".join(lines[:_TRANSCRIPT_LINES]) + (f"\n… ({more} more lines)" if more > 0 else "")
    head = (
        f"`{r['scenario_id']}` ({r['stratum']}/{r['persona']}): outcome **{outcome(r)}**, "
        f"final {r.get('final_intent')} ({r.get('final_reason')}), "
        f"agreement_valid={r.get('agreement_valid')}, leaks={r.get('sensitive_leaks')}, "
        f"unverified={r.get('unverified_figures_spoken')}, turns={r.get('turns_to_outcome')}"
    )
    return f"{head}\n\n```text\n{body}\n```\n"


def build_report(
    arm_dirs: dict[str, Path],
    judges: dict[str, Path],
    *,
    decision: str = "",
    notes: str = "",
) -> tuple[str, dict[str, Any]]:
    """Markdown + JSON for the arms in ``arm_dirs`` (first arm = baseline A)."""
    raw = {a: load_scenario_results(d) for a, d in arm_dirs.items()}
    ids = common_ids(raw)
    by_arm = {a: [_ok(rs)[i] for i in ids] for a, rs in raw.items()}
    rows = {a: arm_row(rs) for a, rs in by_arm.items()}
    judged = {k: load_judge(p) for k, p in judges.items()}
    base_arm = next(iter(arm_dirs))
    vs_a = {a: judged.get(f"{a}:{base_arm}") for a in arm_dirs}
    checks = {
        a: adoption_checks(rows[a], rows[base_arm], (vs_a[a] or {}).get("a_win_rate"))
        for a in arm_dirs
        if a != base_arm
    }

    out: list[str] = []
    out.append(f"# A/B: policy vs H3 vs ReAct vs LLM-only ({len(ids)} common scenarios)\n")
    if notes:
        out.append(notes.strip() + "\n")
    out.append("## Runs\n")
    out.append("| arm | run dir | completed / total | agent | nlg |")
    out.append("|---|---|---|---|---|")
    for a, d in arm_dirs.items():
        run_json = d / "run.json"
        meta = json.loads(run_json.read_text(encoding="utf-8")) if run_json.exists() else {}
        total = len(raw[a])
        done = len(_ok(raw[a]))
        out.append(f"| {a} | `{d}` | {done} / {total} | {meta.get('agent')} | {meta.get('nlg')} |")
    out.append("")
    out.append("## Results (common scenarios only)\n")
    out.append(
        "| arm | n | leaks | unverified | agreement_valid | deal rate (ZOPA) | no-deal correct "
        "| escalation correct | surplus | turns | LLM calls/turn | server_total p50 / p95 ms "
        "| naturalness win-rate vs A |"
    )
    out.append("|" + "---|" * 13)
    for a, row in rows.items():
        out.append(
            f"| {a} | {row['n']} | {row['sensitive_leaks']} | {row['unverified_figures_spoken']} "
            f"| {_fmt(row['agreement_valid'], 2)} (n={row['agreement_valid_n']}) "
            f"| {_rate(row, 'deal_rate_given_zopa')} | {_rate(row, 'no_deal_correct')} "
            f"| {_rate(row, 'escalation_correct')} "
            f"| {_fmt(row['surplus_captured'], 3)} (n={row['surplus_n']}) "
            f"| {_fmt(row['turns_mean'], 1)} | {_fmt(row['llm_calls_per_turn_mean'], 2)} "
            f"| {_fmt(row['server_total_ms_p50'], 0)} / {_fmt(row['server_total_ms_p95'], 0)} "
            f"| {'(baseline)' if a == base_arm else _win(vs_a[a])} |"
        )
    out.append("")
    other = {k: v for k, v in judged.items() if not k.endswith(f":{base_arm}")}
    if other:
        out.append("Other pairwise judgements (first arm's win-rate over decisive pairs):\n")
        for k, j in other.items():
            out.append(f"- {k.replace(':', ' vs ')}: {_win(j)}")
        out.append("")
    out.append(f"## Pre-registered adoption rule (each arm vs {base_arm})\n")
    names = list(next(iter(checks.values())).keys()) if checks else []
    out.append("| arm | " + " | ".join(names) + " | adopt? |")
    out.append("|" + "---|" * (len(names) + 2))
    for a, c in checks.items():
        cells = " | ".join("pass" if c[n] else "**fail**" for n in names)
        out.append(f"| {a} | {cells} | {'yes' if all(c.values()) else 'no'} |")
    out.append("")
    if decision:
        out.append("## Decision\n")
        out.append(decision.strip() + "\n")
    out.append("## Transcripts (one representative, one worst per arm)\n")
    for a, rs in by_arm.items():
        if not rs:
            continue
        rep, worst = pick_transcripts(rs)
        out.append(f"### {a}: representative\n")
        out.append(_transcript_md(rep))
        out.append(f"### {a}: worst\n")
        out.append(_transcript_md(worst))
    data = {
        "common_scenarios": ids,
        "rows": rows,
        "judges": judged,
        "adoption": {a: {"checks": c, "adopt": all(c.values())} for a, c in checks.items()},
    }
    return "\n".join(out), data


def _pairs(values: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for v in values:
        key, _, path = v.partition("=")
        if not path:
            raise SystemExit(f"expected NAME=PATH, got {v!r}")
        out[key] = Path(path)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="A/B report over eval.run_eval arm runs")
    p.add_argument("--arm", action="append", required=True, help="NAME=RUN_DIR; first = baseline")
    p.add_argument("--judge", action="append", default=[], help="X:Y=JUDGE_DIR (X rated vs Y)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--decision", type=Path, default=None, help="markdown paragraph to include")
    p.add_argument("--notes", type=Path, default=None, help="markdown preamble to include")
    args = p.parse_args(argv)
    md, data = build_report(
        _pairs(args.arm),
        _pairs(args.judge),
        decision=args.decision.read_text(encoding="utf-8") if args.decision else "",
        notes=args.notes.read_text(encoding="utf-8") if args.notes else "",
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.md").write_text(md + "\n", encoding="utf-8")
    (args.out / "summary.json").write_text(
        json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
