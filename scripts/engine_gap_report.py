"""Gap report: the frozen take-home generator (``feasibility.legacy``) vs the brute-force oracle.

Runs ``--cases`` seeded small random cases (``tests/engine/gen_cases.py``, small mode)
through ``legacy.legacy_evaluate`` and ``tests/engine/oracle.solve`` and counts:

- false infeasible: legacy says infeasible, a legal feasible schedule exists;
- worse Φ*: both feasible, legacy's cumulative-fee vector is lexicographically below Φ*;
- rescue over-estimates: both infeasible, legacy's lump / increment is above the true
  minimum (or legacy calls it unfixable when it is not);
- consistency checks that must be zero: legacy-feasible but oracle-infeasible, illegal
  legacy vectors, legacy Φ not matching the oracle's Φ for the same vector, rescue
  under-estimates, validator violations on oracle schedules.

Writes ``summary.md`` (and ``summary.json``) with 3 minimal examples per gap. Evidence
only: nothing in the engine is changed by this script.

    uv run python scripts/engine_gap_report.py --cases 5000 --seed 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from random import Random
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapter.validator import validate  # noqa: E402
from feasibility.engine import Result  # noqa: E402
from feasibility.legacy import legacy_candidates, legacy_evaluate  # noqa: E402
from feasibility.models import CreditorRules  # noqa: E402
from feasibility.problem import Problem, build_problem  # noqa: E402
from feasibility.shapes import compute_floors  # noqa: E402
from tests.engine import gen_cases, oracle  # noqa: E402

UNFIXABLE_PREFIX = "no additional funding"


@dataclass
class Row:
    """One case's legacy vs oracle outcome."""

    case: dict[str, Any]
    fpd: date
    S: int
    F: int
    k_max: int
    M: int
    legacy: Result
    orc: oracle.OracleResult
    legacy_vec: tuple[int, ...] | None
    legacy_phi: tuple[int, ...] | None

    def size_key(self) -> tuple[int, ...]:
        return (self.S, self.k_max, self.M, len(self.case["client"]["ledger"]))


def legacy_phi(problem: Problem, result: Result) -> tuple[int, ...] | None:
    """Cumulative fee by each cadence date from a legacy schedule."""
    if result.schedule is None:
        return None
    fee_on: dict[date, int] = {r.date: r.program_fee_cents for r in result.schedule}
    out: list[int] = []
    run = 0
    for t in problem.cadence:
        run += fee_on.get(t, 0)
        out.append(run)
    return tuple(out)


def run_case(case: dict[str, Any]) -> Row:
    """Evaluate one case both ways."""
    client, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    S = problem.offer_total(offer.settlement_bp)
    F = problem.program_fee_cents
    leg = legacy_evaluate(problem, offer.settlement_bp)
    orc = oracle.solve(client, rules, problem.first_payment_date, S, F)
    vec = (
        tuple(r.creditor_payment_cents for r in leg.schedule if r.creditor_payment_cents > 0)
        if leg.schedule is not None
        else None
    )
    return Row(
        case,
        problem.first_payment_date,
        S,
        F,
        problem.k_max,
        problem.M,
        leg,
        orc,
        vec,
        legacy_phi(problem, leg),
    )


def _legacy_rescue(result: Result) -> tuple[int | None, int | None]:
    """Legacy lump / increment, with None for 'unfixable'."""
    af = result.additional_funds
    assert af is not None
    lump = None if af.lump_sum.reason.startswith(UNFIXABLE_PREFIX) else af.lump_sum.amount_cents
    incr = (
        None
        if af.monthly_increment.reason.startswith(UNFIXABLE_PREFIX)
        else af.monthly_increment.amount_cents
    )
    return lump, incr


def _over(legacy: int | None, true: int | None) -> bool:
    """Legacy asks for more than needed (None = unfixable = infinite)."""
    if true is None:
        return False
    return legacy is None or legacy > true


def _under(legacy: int | None, true: int | None) -> bool:
    if legacy is None:
        return False
    return true is None or legacy < true


def compare(rows: list[Row]) -> dict[str, list[Row]]:
    """Bucket every case; the 'bug_*' buckets must stay empty."""
    b: dict[str, list[Row]] = {
        k: []
        for k in (
            "false_infeasible",
            "worse_phi",
            "rescue_over_lump",
            "rescue_over_increment",
            "bug_legacy_feasible_oracle_not",
            "bug_illegal_legacy_vector",
            "bug_legacy_phi_mismatch",
            "bug_legacy_phi_above_oracle",
            "bug_rescue_under",
            "bug_validator_on_oracle",
        )
    }
    for r in rows:
        client, _, rules = gen_cases.case_to_objects(r.case)
        if r.orc.feasible:
            assert r.orc.rows is not None
            if validate(r.orc.rows, client, r.S, r.F, rules, r.fpd):
                b["bug_validator_on_oracle"].append(r)
        if r.legacy.feasible:
            assert r.legacy_vec is not None and r.legacy_phi is not None
            if not r.orc.feasible:
                b["bug_legacy_feasible_oracle_not"].append(r)
                continue
            if not oracle.is_legal(r.legacy_vec, r.S, r.k_max, rules):
                b["bug_illegal_legacy_vector"].append(r)
            own = oracle.phi_of(client, rules, r.fpd, r.S, r.F, r.legacy_vec)
            if own != r.legacy_phi:
                b["bug_legacy_phi_mismatch"].append(r)
            assert r.orc.phi is not None
            if r.legacy_phi < r.orc.phi:
                b["worse_phi"].append(r)
            elif r.legacy_phi > r.orc.phi:
                b["bug_legacy_phi_above_oracle"].append(r)
        elif r.orc.feasible:
            b["false_infeasible"].append(r)
        else:
            lump, incr = _legacy_rescue(r.legacy)
            if _over(lump, r.orc.lump_cents):
                b["rescue_over_lump"].append(r)
            if _over(incr, r.orc.increment_cents):
                b["rescue_over_increment"].append(r)
            if _under(lump, r.orc.lump_cents) or _under(incr, r.orc.increment_cents):
                b["bug_rescue_under"].append(r)
    return b


def _fmt_case(r: Row) -> str:
    c = r.case
    ru = c["rules"]
    if ru["even_pays"]:
        structure = "even"
    else:
        structure = "balloon" if ru["is_ballooning_allowed"] else "staircase"
    debits = [(d, a) for d, a, t in c["client"]["ledger"] if t == "debit"]
    lines = [
        f"- `{c['id']}`: S={r.S}, F={r.F}, k_max={r.k_max}, M={r.M}, {structure}, "
        f"min={ru['min_payment_cents']}, tokens={ru['max_token_pays']}, "
        f"tiers={ru['min_payment_tiers']}, segments={ru['max_segments']}, "
        f"bank_fee={ru['bank_fee_cents']}",
        f"  - client: balance {c['client']['current_balance_cents']}, draft "
        f"{c['client']['draft_amount_cents']} on {c['client']['first_draft_date']}.."
        f"{c['client']['last_draft_date']}, debits {debits}, first payment "
        f"{c['first_payment_date'] or 'default'}",
    ]
    if r.legacy.feasible:
        lines.append(f"  - legacy: {list(r.legacy_vec or ())}, Φ={list(r.legacy_phi or ())}")
    else:
        lump, incr = _legacy_rescue(r.legacy)
        lines.append(f"  - legacy: infeasible, lump={lump}, increment={incr}")
    if r.orc.feasible:
        lines.append(f"  - oracle: {list(r.orc.best or ())}, Φ*={list(r.orc.phi or ())}")
    else:
        lines.append(
            f"  - oracle: infeasible, lump={r.orc.lump_cents}, increment={r.orc.increment_cents}"
        )
    return "\n".join(lines)


def _nondecreasing(S: int, k: int, lo: int = 0) -> Iterator[tuple[int, ...]]:
    if k == 1:
        if S >= lo:
            yield (S,)
        return
    for p in range(lo, S // k + 1):
        for rest in _nondecreasing(S - p, k - 1, p):
            yield (p, *rest)


def _cap_phi(v: tuple[int, ...], caps: list[int], F: int, tail: int) -> tuple[int, ...] | None:
    """Φ in the pure prefix-cap model (None if some window goes negative)."""
    slack, run = [], 0
    for j, p in enumerate(v[:-1]):
        run += p
        slack.append(caps[j] - run)
    slack.append(tail)
    if min(slack) < 0:
        return None
    out, m = [], F
    for x in reversed(slack):
        m = min(m, x)
        out.append(m)
    return tuple(reversed(out))


def cap_model_sweep(n: int, seed: int, k_hi: int, s_hi: int) -> dict[str, int]:
    """Generator-level check without a ledger: random prefix caps, fixed k.

    By the prefix-cap identity a length-k vector is feasible iff ``P_j <= cap_j`` for
    j < k (the tail only depends on k), and its Φ is the suffix-min of the slacks. So
    this compares the legacy family to *every* legal vector on many more cap shapes
    than ledgers produce. Counts: ``feasible`` (some legal vector fits), ``miss``
    (legacy has none that fits), ``worse_phi`` (legacy's best Φ below the true best).
    """
    rng = Random(seed)
    out = {"draws": n, "feasible": 0, "miss": 0, "worse_phi": 0}
    for _ in range(n):
        k = rng.randint(3, k_hi)
        S = rng.randint(k, s_hi)
        base = rng.randint(0, 8)
        tiers = sorted(
            (rng.randint(2, k), base + rng.randint(1, 9)) for _ in range(rng.choice([0, 0, 1, 2]))
        )
        rules = CreditorRules(
            k, k, base, rng.randint(0, k), tiers, False, rng.random() < 0.2,
            rng.choice([2, 3, 3, 4]), 0, program_fee_bp=0,
        )
        caps = [rng.randint(0, S + 10) for _ in range(k)]
        F = rng.randint(0, 15)
        tail = rng.randint(F, F + 10)
        best = None
        for v in _nondecreasing(S, k):
            if oracle.is_legal(v, S, k, rules):
                phi = _cap_phi(v, caps, F, tail)
                if phi is not None and (best is None or phi > best):
                    best = phi
        if best is None:
            continue
        out["feasible"] += 1
        fam = legacy_candidates(S, k, compute_floors(rules, k), rules)
        phis = [_cap_phi(c.payments, caps, F, tail) for c in fam if c.k == k]
        phis = [x for x in phis if x is not None]
        if not phis:
            out["miss"] += 1
        elif max(phis) < best:
            out["worse_phi"] += 1
    return out


TITLES = {
    "false_infeasible": "False infeasible (legacy walks away from a legal deal)",
    "worse_phi": "Worse Φ* (legacy collects the fee later than possible)",
    "rescue_over_lump": "Rescue over-estimate: lump sum",
    "rescue_over_increment": "Rescue over-estimate: monthly increment",
}


def render(
    buckets: dict[str, list[Row]],
    rows: list[Row],
    args: argparse.Namespace,
    secs: float,
    caps: list[dict[str, int]],
) -> str:
    """Markdown summary with counts and minimal examples."""
    n = len(rows)
    feas_l = sum(r.legacy.feasible for r in rows)
    feas_o = sum(r.orc.feasible for r in rows)
    infeas_both = sum(not r.legacy.feasible and not r.orc.feasible for r in rows)
    both = sum(r.legacy.feasible and r.orc.feasible for r in rows)
    out = [
        f"# Engine gap report: legacy generator vs brute-force oracle ({date.today():%Y-%m-%d})",
        "",
        f"Command: `uv run python scripts/engine_gap_report.py --cases {args.cases} "
        f"--seed {args.seed}` ({secs:.1f} s). Cases: small seeded random "
        "(`tests/engine/gen_cases.py`, small mode: S ≤ 400 cents, k_max ≤ 6, M ≤ 8, all "
        "three structures, tiers, token limits, committed debits, mid-month and EOM starts).",
        "",
        "Legacy = `feasibility/legacy.py` (the take-home candidate family, frozen at Phase 55) "
        "scored by the engine. Oracle = `tests/engine/oracle.py` (every vector legal under "
        "binding rules 1–9, suffix-min fee, Part 2 by binary search). Evidence only; nothing "
        "is fixed here.",
        "",
        "| | count |",
        "|---|---|",
        f"| cases | {n} |",
        f"| oracle skipped (over enumeration budget) | {args.skipped} |",
        f"| legacy feasible / oracle feasible | {feas_l} / {feas_o} |",
        f"| both feasible (Φ* compared) | {both} |",
        f"| both infeasible (rescue compared) | {infeas_both} |",
        f"| … of which k_max = 0 (no cadence date before the horizon or a zero payment cap) | "
        f"{sum(not r.orc.feasible and r.k_max == 0 for r in rows)} |",
        f"| … of which oracle lump fixable / increment fixable | "
        f"{sum(not r.orc.feasible and r.orc.lump_cents is not None for r in rows)} / "
        f"{sum(not r.orc.feasible and r.orc.increment_cents is not None for r in rows)} |",
    ]
    for key, title in TITLES.items():
        denom = n if key == "false_infeasible" else (both if key == "worse_phi" else infeas_both)
        out.append(f"| {title} | {len(buckets[key])} of {denom} |")
    out += ["", "## Consistency checks (must all be 0)", "", "| check | count |", "|---|---|"]
    for key, rs in buckets.items():
        if key.startswith("bug_"):
            out.append(f"| {key[4:].replace('_', ' ')} | {len(rs)} |")
    out += [
        "",
        "## Prefix-cap model sweep (generator only, no ledger)",
        "",
        "Random prefix caps for a fixed k: a vector fits iff `P_j <= cap_j` for j < k, "
        "Φ is the suffix-min of the slacks (prefix-cap identity, `feasibility/problem.py`). "
        "Every non-decreasing vector legal under rules 1–9 is enumerated and compared with "
        "the legacy family for the same k.",
        "",
        "| draws | k ≤ | S ≤ | feasible | legacy miss | legacy worse Φ |",
        "|---|---|---|---|---|---|",
    ]
    for c in caps:
        out.append(
            f"| {c['draws']} | {c['k_hi']} | {c['s_hi']} | {c['feasible']} | {c['miss']} | "
            f"{c['worse_phi']} |"
        )
    for key, title in TITLES.items():
        out += ["", f"## {title}", ""]
        rs = sorted(buckets[key], key=Row.size_key)[:3]
        out.append("\n".join(_fmt_case(r) for r in rs) if rs else "None found.")
    for key, rs in buckets.items():
        if key.startswith("bug_") and rs:
            out += ["", f"## {key}", ""]
            out.append("\n".join(_fmt_case(r) for r in sorted(rs, key=Row.size_key)[:3]))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cases", type=int, default=5000)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", type=Path, default=None, help="output dir (default docs/eval/...)")
    p.add_argument("--cap-draws", type=int, default=4000, help="prefix-cap model draws per size")
    args = p.parse_args(argv)
    out_dir = args.out or ROOT / "docs" / "eval" / f"engine_gap_{date.today():%Y%m%d}"
    t0 = time.perf_counter()
    rows: list[Row] = []
    skipped = 0
    for case in gen_cases.random_cases(args.seed, args.cases, small=True, prefix="g"):
        try:
            rows.append(run_case(case))
        except oracle.OracleTooBig:
            skipped += 1
    args.skipped = skipped
    buckets = compare(rows)
    caps = []
    for k_hi, s_hi in ((6, 40), (8, 60)):
        sweep = cap_model_sweep(args.cap_draws, args.seed, k_hi, s_hi)
        caps.append({**sweep, "k_hi": k_hi, "s_hi": s_hi})
    secs = time.perf_counter() - t0
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text(render(buckets, rows, args, secs, caps))
    counts: dict[str, Any] = {k: len(v) for k, v in buckets.items()}
    counts.update(cases=len(rows), skipped=skipped, seed=args.seed, cap_model=caps)
    (out_dir / "summary.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
    print(json.dumps(counts, sort_keys=True))
    print(f"wrote {out_dir / 'summary.md'}")
    return 1 if any(v for k, v in counts.items() if k.startswith("bug_")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
