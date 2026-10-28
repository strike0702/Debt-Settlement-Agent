"""Affordability-scan timing for the engine: one ``Problem``, 100 settlement points.

Times what ``app.adapter.engine_adapter.affordability`` does on a cache miss
(``build_problem`` once, then ``engine.evaluate`` for bp 100..10000) on fixed targets:

- ``demo``: ``fixtures/demo`` with even rules (the Phase 55 figure), and the same client
  with 4-level staircase rules and a tier (exercises the Phase 57b search);
- the 7 ``fixtures/scenarios`` with their true ``sim.json`` rules;
- the 5 ``fixtures/engine`` cases and the 600 golden random cases, each as a full scan;
- ``--adversarial N`` Phase 57 adversarial cases (reported, not part of the budget).

Prints JSON (median per fixed target over ``--repeat`` runs; p50 / p95 / max over the
case sets). Read-only timing: never changes engine output. Phase 57b, ``bench.md``.
``--objective`` / ``--fee-policy`` (Phase 59) pick the engine mode every scan runs under
(default ``fee_first`` / ``front_loaded``, the 57b numbers). ``--scan feasible`` times the
adapter's scan since Phase 59 (``engine.is_feasible`` per point, no ranking).

    uv run python scripts/engine_bench.py --repeat 15 --adversarial 300
    uv run python scripts/engine_bench.py --objective client_first --adversarial 300
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from feasibility.engine import evaluate, is_feasible  # noqa: E402
from feasibility.models import CreditorRules  # noqa: E402
from feasibility.problem import build_problem  # noqa: E402
from tests.engine import gen_cases  # noqa: E402
from tests.engine.golden import build_golden as bg  # noqa: E402

SCAN = range(100, 10001, 100)
SINGLE: list[float] = []  # every timed single evaluate (``--scan evaluate``)
MODE = {"objective": "fee_first", "fee_policy": "front_loaded", "scan": "evaluate"}


def scan_secs(client: Any, rules: CreditorRules, fpd: Any, cb: int, ob: int) -> float:
    """Wall time of one cold 100-point scan (Problem build included)."""
    t = time.perf_counter()
    problem = build_problem(
        client, rules, fpd, creditor_balance_cents=cb, original_balance_cents=ob,
        fee_policy=MODE["fee_policy"],
    )
    if MODE["scan"] == "feasible":  # what engine_adapter.affordability does since P59
        for bp in SCAN:
            is_feasible(problem, bp)
    else:
        for bp in SCAN:
            t1 = time.perf_counter()
            evaluate(problem, bp, MODE["objective"])
            SINGLE.append(time.perf_counter() - t1)
    return time.perf_counter() - t


def case_secs(case: dict[str, Any]) -> float:
    """One cold scan for a stored case dict (its own ``settlement_bp`` is ignored)."""
    client, offer, rules = gen_cases.case_to_objects({**case, "settlement_bp": 100})
    return scan_secs(
        client, rules, offer.first_payment_date,
        offer.creditor_balance_cents, offer.original_balance_cents,
    )


def demo_targets() -> dict[str, tuple[Any, ...]]:
    """The demo client with even rules and with 4-level staircase rules."""
    from app.domain.scenario import load_scenario

    call = load_scenario(ROOT / "fixtures" / "demo")
    base = dict(
        max_terms=8, max_payments=8, min_payment_cents=5000, max_token_pays=8,
        bank_fee_cents=call.bank_fee_cents,
        program_fee_bp=bg.to_bp(call.program_fee_pct),
    )
    even = CreditorRules(**base, min_payment_tiers=[], even_pays=True,
                         is_ballooning_allowed=False, max_segments=4)
    stair = CreditorRules(**base, min_payment_tiers=[(5, 9000)], even_pays=False,
                          is_ballooning_allowed=False, max_segments=4)
    args = (None, call.creditor_balance_cents, call.original_balance_cents)
    return {
        "demo_even": (call.client, even, *args),
        "demo_staircase4": (call.client, stair, *args),
    }


def dist(xs: list[float]) -> dict[str, float]:
    """p50 / p95 / max in milliseconds, and how many scans exceed the 150 ms budget."""
    s = sorted(xs)
    return {
        "n": len(s),
        "p50_ms": round(s[len(s) // 2] * 1000, 2),
        "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))] * 1000, 2),
        "max_ms": round(s[-1] * 1000, 2),
        "over_150ms": sum(x > 0.150 for x in s),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repeat", type=int, default=15)
    p.add_argument("--adversarial", type=int, default=300)
    p.add_argument("--seed", type=int, default=57)
    p.add_argument("--objective", choices=("fee_first", "client_first"), default="fee_first")
    p.add_argument(
        "--fee-policy", choices=("front_loaded", "proportional"), default="front_loaded"
    )
    p.add_argument("--scan", choices=("evaluate", "feasible"), default="evaluate")
    args = p.parse_args(argv)
    MODE.update(objective=args.objective, fee_policy=args.fee_policy, scan=args.scan)

    out: dict[str, Any] = {"mode": dict(MODE), "fixed_median_ms": {}, "fixed_max_ms": {}}
    fixed: dict[str, tuple[Any, ...]] = dict(demo_targets())
    for name in bg.SCENARIOS:
        c = bg.scenario_case(name)
        client, offer, rules = gen_cases.case_to_objects({**c, "settlement_bp": 100})
        fixed[name] = (client, rules, offer.first_payment_date,
                       offer.creditor_balance_cents, offer.original_balance_cents)
    for name, a in fixed.items():
        scan_secs(*a)  # warm imports and caches outside the timed runs
        runs = [scan_secs(*a) for _ in range(args.repeat)]
        out["fixed_median_ms"][name] = round(statistics.median(runs) * 1000, 2)
        out["fixed_max_ms"][name] = round(max(runs) * 1000, 2)

    if SINGLE:
        out["single_evaluate_fixed"] = dist(SINGLE)
        SINGLE.clear()
    fx = [case_secs(bg.fixture_case(n)) for n in bg.FIXTURE_CASES]
    out["engine_fixtures"] = dist(fx)
    golden = gen_cases.random_cases(bg.RANDOM_SEED, bg.RANDOM_COUNT)
    gt = [(case_secs(c), c["id"]) for c in golden]
    out["golden_random"] = dist([t for t, _ in gt])
    out["golden_random_slowest"] = [[i, round(t * 1000, 1)] for t, i in sorted(gt)[-5:]]
    if SINGLE:
        out["single_evaluate_fixtures_golden"] = dist(SINGLE)
        SINGLE.clear()
    if args.adversarial:
        adv = gen_cases.adversarial_cases(args.seed, args.adversarial)
        at = [(case_secs(c), c["id"]) for c in adv]
        out["adversarial"] = dist([t for t, _ in at])
        out["adversarial_slowest"] = [[i, round(t * 1000, 1)] for t, i in sorted(at)[-5:]]
    if SINGLE:
        out["single_evaluate_adversarial"] = dist(SINGLE)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
