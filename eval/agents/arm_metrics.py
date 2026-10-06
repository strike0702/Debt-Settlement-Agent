"""Per-arm cost metrics for the A/B: LLM calls per turn and per-turn latency.

Reads the per-call result dicts written by ``eval.run_eval.run_one_scenario``
(``llm_calls_per_turn``, ``turn_latency_ms``) and returns a small dict that the
runner stores in ``run.json["arm_metrics"]``. Deliberately separate from
``eval.metrics.aggregate`` so the gated summary table stays byte-identical for
the policy arm. Not a gate.
"""

from __future__ import annotations

from typing import Any

from eval.metrics import _percentile


def arm_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean / p95 LLM calls per agent turn and turn latency p50 / p95 over ``status=ok``."""
    calls: list[float] = []
    latency: list[float] = []
    for r in results:
        if r.get("status") != "ok":
            continue
        calls.extend(float(c) for c in r.get("llm_calls_per_turn") or [])
        latency.extend(float(ms) for ms in r.get("turn_latency_ms") or [] if ms is not None)
    return {
        "turns": len(calls),
        "llm_calls_per_turn_mean": sum(calls) / len(calls) if calls else None,
        "llm_calls_per_turn_p95": _percentile(calls, 0.95),
        "llm_calls_per_turn_max": max(calls) if calls else None,
        "turn_latency_ms_p50": _percentile(latency, 0.50),
        "turn_latency_ms_p95": _percentile(latency, 0.95),
    }
