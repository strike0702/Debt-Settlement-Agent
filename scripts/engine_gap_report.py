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

``--mode full`` (Phase 57) compares legacy with the exact full-size reference
(``tests/engine/reference.py``, per-case logic in ``tests/engine/hunt.py``): the
reference is first checked against the oracle on small seeded sets, then legacy is
compared on the golden inputs, seeded full-size random cases, adversarial cases and a
cap-free structural dominance sweep; misses are shrunk and the smallest reported in
``docs/eval/engine_gap_<date>_full/``. Since Phase 57b the same sets also compare the
engine itself (``engine.evaluate``, exact search) with the reference (``engine_*``
columns; every count must be 0), and every legacy-miss case and verified structural
witness is written to ``witnesses.jsonl`` (the source of
``tests/engine/golden/witnesses_57.jsonl``).

    uv run python scripts/engine_gap_report.py --cases 5000 --seed 1
    uv run python scripts/engine_gap_report.py --mode full --seed 57
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
from tests.engine import gen_cases, hunt, oracle, reference  # noqa: E402

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


# --- full mode (Phase 57): legacy vs the exact reference at full size ---------------

FULL_TITLES = {
    "false_infeasible": "False infeasible (legacy walks away from a legal deal)",
    "worse_phi": "Worse Φ* (legacy collects the fee later than possible)",
    "rescue_over_lump": "Rescue over-estimate: lump sum",
    "rescue_over_increment": "Rescue over-estimate: monthly increment",
}


def _refcheck_one(case: dict[str, Any]) -> tuple[bool, bool]:
    """(reference agrees with the oracle, oracle feasible) on one small case."""
    client, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client, rules, offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    S = problem.offer_total(offer.settlement_bp)
    F = problem.program_fee_cents
    r = reference.solve(problem, S)
    o = oracle.solve(client, rules, problem.first_payment_date, S, F)
    got = (r.feasible, r.phi, r.lump_cents, r.increment_cents)
    return got == (o.feasible, o.phi, o.lump_cents, o.increment_cents), o.feasible


def golden_cases() -> list[dict[str, Any]]:
    """Every golden input: 5 fixtures, 7 scenarios x 100 bp, 600 random (embedded inputs)."""
    from tests.engine.golden.build_golden import GOLDEN, expand

    out: list[dict[str, Any]] = []
    for line in GOLDEN.read_text().splitlines():
        rec = json.loads(line)
        for c in expand(rec):
            out.append({**c, "id": f"{c['id']}@{c['settlement_bp']}"} if "bps" in rec else c)
    return out


def structural_draw(rng: Random, i: int, small: bool) -> tuple[dict[str, Any], int, int]:
    """Random staircase rules and S close to the floors' sum (where pins bite)."""
    k = rng.randint(3, 10 if small else 24)
    segs = rng.choice([3, 4])
    if small:
        base = rng.randint(1, 30)
    else:
        base = rng.randint(10, 150) * 100 + rng.choice([0, rng.randint(1, 99)])
    tiers = sorted(
        [rng.randint(2, k), base + rng.choice([rng.randint(1, max(2, base)),
                                               rng.randint(1, 99)])]
        for _ in range(rng.choice([0, 1, 1, 2]))
    )
    rules = {
        "max_terms": k, "max_payments": k, "min_payment_cents": base,
        "max_token_pays": rng.randint(0, k), "min_payment_tiers": tiers,
        "even_pays": False, "is_ballooning_allowed": False, "max_segments": segs,
        "bank_fee_cents": 0 if small else rng.choice([0, rng.randint(1, 1500)]),
    }
    f = gen_cases.adv_floors(base, rules["max_token_pays"], tiers, k)
    floor_sum = sum(f[1:])
    spread = 300 if small else 300_000
    S = floor_sum + rng.choice(
        [rng.randint(0, k * k), rng.randint(0, 20 * k), rng.randint(0, max(1, spread - floor_sum))]
    )
    return rules, k, S


def _structural_one(args: tuple[int, int, bool, bool]) -> dict[str, Any]:
    seed, i, small, flat = args
    rng = Random(seed * 1_000_003 + i)
    rules_d, k, S = structural_draw(rng, i, small)
    if flat:  # same draw with the tiers removed (floors: base, +1 after the token limit)
        rules_d = {**rules_d, "min_payment_tiers": []}
    rules = CreditorRules(**{**rules_d, "program_fee_bp": 0})
    n_fam, misses = hunt.structural_misses(rules, S, k)
    out: dict[str, Any] = {"family": n_fam, "misses": len(misses), "rules": rules_d, "S": S,
                           "k": k, "small": small, "flat": flat,
                           "tiers": bool(rules_d["min_payment_tiers"])}
    if misses:
        vec = min(misses, key=lambda v: (sum(v), v))
        t1 = date(2027, rng.randint(1, 12), rng.choice([1, 15, 28]))
        F = 0 if small else rng.choice([0, rng.randint(1, 20000)])
        case = hunt.structural_witness_case(f"s{seed}_{i:05d}", rules_d, vec, t1=t1, F=F)
        rec = hunt.compare_case(case)
        out.update(vec=list(vec), case=case, rec=rec,
                   verified="false_infeasible" in rec["kinds"] and not rec["bugs"])
    return out


def _compare_safe(case: dict[str, Any]) -> dict[str, Any]:
    t = time.perf_counter()
    rec = hunt.compare_case(case)
    rec["secs"] = time.perf_counter() - t
    rec["case"] = case
    return rec


def _prune_check_one(case: dict[str, Any]) -> bool:
    a = hunt.compare_case(case)
    b = hunt.compare_case(case, prune=False)
    keys = ("ref_feasible", "ref_phi", "ref_lump", "ref_increment")
    return all(a.get(x) == b.get(x) for x in keys)


def _fmt_full(rec: dict[str, Any]) -> str:
    c = rec["case"]
    ru = c["rules"]
    debits = [(d, a) for d, a, t in c["client"]["ledger"] if t == "debit"]
    credits = [(d, a) for d, a, t in c["client"]["ledger"] if t == "credit"]
    lines = [
        f"- `{rec['id']}`: S={rec['S']}, F={rec['F']}, k_max={rec['k_max']}, M={rec['M']}, "
        f"{rec['structure']}, min={ru['min_payment_cents']}, tokens={ru['max_token_pays']}, "
        f"tiers={ru['min_payment_tiers']}, segments={ru['max_segments']}, "
        f"bank_fee={ru['bank_fee_cents']}, first payment {c['first_payment_date'] or 'default'}",
        f"  - client: balance {c['client']['current_balance_cents']}, as of "
        f"{c['client']['as_of_date']}, credits {credits}, debits {debits}",
    ]
    if rec["legacy_feasible"]:
        lines.append(f"  - legacy: {rec['legacy_vec']}, Φ={rec['legacy_phi']}")
    else:
        lines.append(f"  - legacy: infeasible, lump={rec.get('legacy_lump')}, "
                     f"increment={rec.get('legacy_increment')}")
    if rec["ref_feasible"]:
        lines.append(f"  - reference: {rec['ref_vec']}, Φ*={rec['ref_phi']} "
                     f"(runs {rec.get('runs')}, lifted levels {rec.get('lifted')})")
    else:
        lines.append(
            f"  - reference: infeasible, lump={rec['ref_lump']} via {rec['ref_lump_vec']}, "
            f"increment={rec['ref_increment']} via {rec['ref_increment_vec']} "
            f"(runs {rec.get('runs')}, lifted levels {rec.get('lifted')})"
        )
    return "\n".join(lines)


def run_full(args: argparse.Namespace) -> int:
    """Golden + random + adversarial + structural sweep; writes summary.md/json."""
    from multiprocessing import Pool

    out_dir = args.out or ROOT / "docs" / "eval" / f"engine_gap_{date.today():%Y%m%d}_full"
    t_all = time.perf_counter()
    timing: dict[str, float] = {}
    summary: dict[str, Any] = {"args": {k: (str(v) if isinstance(v, Path) else v)
                                        for k, v in vars(args).items()}}
    with Pool(args.workers) as pool:
        # 1. Reference vs oracle on small cases.
        t = time.perf_counter()
        refcheck = []
        for spec in args.ref_check.split(","):
            seed, n = (int(x) for x in spec.split(":"))
            res = pool.map(_refcheck_one, gen_cases.random_cases(seed, n, small=True, prefix="g"),
                           chunksize=64)
            refcheck.append({"seed": seed, "cases": n, "disagree": sum(not a for a, _ in res),
                             "feasible": sum(b for _, b in res)})
        timing["reference_vs_oracle"] = time.perf_counter() - t
        summary["reference_vs_oracle"] = refcheck

        # 2. The three full-size case sets.
        sets = {
            "golden": golden_cases(),
            "random": gen_cases.random_cases(args.seed, args.random, prefix="f"),
            "adversarial": gen_cases.adversarial_cases(args.seed, args.adversarial),
        }
        recs: dict[str, list[dict[str, Any]]] = {}
        for name, cases in sets.items():
            t = time.perf_counter()
            recs[name] = pool.map(_compare_safe, cases, chunksize=16)
            timing[name] = time.perf_counter() - t

        # 3. Pruning self-check (whole family scored) on a prefix of each set.
        t = time.perf_counter()
        pc_cases = [c for cs in sets.values() for c in cs[: args.prune_check]]
        pc = pool.map(_prune_check_one, pc_cases, chunksize=8)
        timing["prune_check"] = time.perf_counter() - t
        summary["prune_check"] = {"cases": len(pc), "disagree": sum(not x for x in pc)}

        # 4. Structural (cap-free) dominance sweep.
        t = time.perf_counter()
        jobs = [(args.seed, i, True, False) for i in range(args.structural_small)]
        jobs += [(args.seed, i + 10**6, False, False) for i in range(args.structural)]
        jobs += [(args.seed, i, True, True) for i in range(args.structural_small)]
        jobs += [(args.seed, i + 10**6, False, True) for i in range(args.structural)]
        struct = pool.map(_structural_one, jobs, chunksize=8)
        timing["structural"] = time.perf_counter() - t

    # 5. Shrink the smallest misses per kind.
    t = time.perf_counter()
    misses: dict[str, list[dict[str, Any]]] = {k: [] for k in hunt.KINDS}
    for name, rs in recs.items():
        for r in rs:
            for kind in r["kinds"]:
                misses[kind].append({**r, "set": name})
    for s in struct:
        if s.get("verified") and not s["flat"]:
            misses["false_infeasible"].append({**s["rec"], "case": s["case"], "set": "structural"})
    shrunk: dict[str, list[dict[str, Any]]] = {}
    for kind, rs in misses.items():
        pool_rs = sorted(rs, key=hunt.size_key)[: args.shrink]
        out = []
        for r in pool_rs:
            case, rec = hunt.shrink(r["case"], kind, budget=args.shrink_budget)
            out.append({**rec, "case": case, "set": r["set"], "from": r["id"]})
        uniq: dict[tuple, dict[str, Any]] = {}
        for r in sorted(out, key=hunt.size_key):
            uniq.setdefault(hunt.size_key(r) + (str(r.get("ref_vec")),), r)
        shrunk[kind] = list(uniq.values())[:3]
    timing["shrink"] = time.perf_counter() - t
    timing["total"] = time.perf_counter() - t_all

    # Counts.
    table: dict[str, dict[str, Any]] = {}
    for name, rs in recs.items():
        row: dict[str, Any] = {
            "cases": len(rs),
            "ref_feasible": sum(r["ref_feasible"] for r in rs),
            "legacy_feasible": sum(r["legacy_feasible"] for r in rs),
            "both_infeasible": sum(not r["ref_feasible"] and not r["legacy_feasible"] for r in rs),
            "max_secs": max(r["secs"] for r in rs),
            "p50_secs": sorted(r["secs"] for r in rs)[len(rs) // 2],
        }
        for kind in hunt.KINDS:
            row[kind] = sum(kind in r["kinds"] for r in rs)
        row["bugs"] = sum(bool(r["bugs"]) for r in rs)
        for kind in hunt.KINDS:
            row[f"engine_{kind}"] = sum(kind in r["engine_kinds"] for r in rs)
        row["engine_bugs"] = sum(bool(r["engine_bugs"]) for r in rs)
        table[name] = row
    by_struct: dict[str, int] = {}
    by_runs: dict[str, int] = {}
    mech: dict[str, int] = {}
    for kind, rs in misses.items():
        for r in rs:
            key = f"{kind} | {r['set']} | {r['structure']} s={r['segments']}"
            by_struct[key] = by_struct.get(key, 0) + 1
            rk = f"{kind} | runs {r.get('runs')}"
            by_runs[rk] = by_runs.get(rk, 0) + 1
            mk = (f"{kind} | lifted {'yes' if r.get('lifted') else 'no'} | legacy family "
                  f"{'empty' if r.get('legacy_empty') else 'not empty'}")
            mech[mk] = mech.get(mk, 0) + 1
    groups = {
        "small": [s for s in struct if s["small"] and not s["flat"]],
        "full": [s for s in struct if not s["small"] and not s["flat"]],
        "small, tiers removed": [s for s in struct if s["small"] and s["flat"]],
        "full, tiers removed": [s for s in struct if not s["small"] and s["flat"]],
        "all draws without tiers": [s for s in struct if not s["tiers"]],
        "all draws with tiers": [s for s in struct if s["tiers"]],
    }

    def sstat(ss: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "draws": len(ss),
            "family_vectors": sum(s["family"] for s in ss),
            "draws_with_miss": sum(s["misses"] > 0 for s in ss),
            "vectors_not_dominated": sum(s["misses"] for s in ss),
            "witness_verified": sum(bool(s.get("verified")) for s in ss),
            "engine_misses": sum(
                bool(s["rec"]["engine_kinds"] or s["rec"]["engine_bugs"])
                for s in ss if s.get("rec")
            ),
        }

    summary.update(
        sets=table, structural={name: sstat(ss) for name, ss in groups.items()},
        by_structure=by_struct, by_runs=by_runs, mechanism=mech,
        timing_secs={k: round(v, 1) for k, v in timing.items()},
        bugs=[{"id": r["id"], "bugs": r["bugs"]} for rs in recs.values() for r in rs if r["bugs"]],
        engine_misses=[
            {"id": r["id"], "kinds": r["engine_kinds"], "bugs": r["engine_bugs"]}
            for rs in [*recs.values(), [s["rec"] for s in struct if s.get("rec")]]
            for r in rs if r["engine_kinds"] or r["engine_bugs"]
        ],
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (out_dir / "summary.md").write_text(render_full(summary, shrunk, args))
    wit = [{"set": name, "kinds": r["kinds"], "case": r["case"]}
           for name, rs in recs.items() for r in rs if r["kinds"]]
    wit += [{"set": "structural", "kinds": s["rec"]["kinds"], "case": s["case"]}
            for s in struct if s.get("verified")]
    (out_dir / "witnesses.jsonl").write_text(
        "".join(json.dumps(w, sort_keys=True, separators=(",", ":")) + "\n" for w in wit)
    )
    print(json.dumps({k: summary[k] for k in ("sets", "structural", "prune_check",
                                               "reference_vs_oracle", "timing_secs")},
                     sort_keys=True))
    print(f"wrote {out_dir / 'summary.md'}")
    bad = summary["bugs"] or summary["engine_misses"] or summary["prune_check"]["disagree"] or any(
        r["disagree"] for r in refcheck
    )
    return 1 if bad else 0


def render_full(summary: dict[str, Any], shrunk: dict[str, list[dict[str, Any]]],
                args: argparse.Namespace) -> str:
    """Markdown for the full-size hunt."""
    a = summary["args"]
    cmd = (f"uv run python scripts/engine_gap_report.py --mode full --seed {a['seed']} "
           f"--random {a['random']} --adversarial {a['adversarial']} --structural "
           f"{a['structural']} --structural-small {a['structural_small']} --ref-check "
           f"{a['ref_check']} --prune-check {a['prune_check']}")
    out = [
        "# Engine gap report, full size: legacy family and engine vs exact reference "
        f"({date.today():%Y-%m-%d})",
        "",
        f"Command: `{cmd}` ({summary['timing_secs']['total']} s on {a['workers']} workers).",
        "",
        "Legacy = `feasibility/legacy.py` (frozen take-home family) scored by the engine. "
        "Reference = `tests/engine/reference.py` (exact over every vector legal under rules "
        "1–9, by a dominating family and branch and bound; exactness argument in its "
        "docstring). Every reference witness is replayed by `oracle.Instance` (independent "
        "ledger replay) and checked by `app/adapter/validator.py`; rescue witnesses are "
        "feasible at exactly L*/X* and not one cent below. Engine = `engine.evaluate` (the "
        "Phase 57b exact search); its columns count the same misses for the engine.",
        "",
        "## Reference vs brute-force oracle (small cases)",
        "",
        "| seed | cases | oracle feasible | disagreements (feasibility, Φ*, L*, X*) |",
        "|---|---|---|---|",
    ]
    for r in summary["reference_vs_oracle"]:
        out.append(f"| {r['seed']} | {r['cases']} | {r['feasible']} | {r['disagree']} |")
    pc = summary["prune_check"]
    out += ["", f"Pruning self-check (whole dominating family scored, no bounds): "
            f"{pc['disagree']} disagreements on {pc['cases']} full-size cases.", "",
            "## Full-size case sets", "",
            "| set | cases | ref feasible | legacy feasible | both infeasible | false infeasible "
            "| worse Φ* | lump over | increment over | bugs | ref p50 / max s |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    eng_rows = ["", "Engine (exact search) vs the reference on the same cases:", "",
                "| set | cases | false infeasible | worse Φ* | lump over | increment over "
                "| bugs (incl. validator) |", "|---|---|---|---|---|---|---|"]
    for name, r in summary["sets"].items():
        out.append(
            f"| {name} | {r['cases']} | {r['ref_feasible']} | {r['legacy_feasible']} | "
            f"{r['both_infeasible']} | {r['false_infeasible']} | {r['worse_phi']} | "
            f"{r['rescue_over_lump']} | {r['rescue_over_increment']} | {r['bugs']} | "
            f"{r['p50_secs']:.3f} / {r['max_secs']:.2f} |"
        )
        eng_rows.append(
            f"| {name} | {r['cases']} | {r['engine_false_infeasible']} | {r['engine_worse_phi']} "
            f"| {r['engine_rescue_over_lump']} | {r['engine_rescue_over_increment']} | "
            f"{r['engine_bugs']} |"
        )
    out += eng_rows
    out += ["", "Golden = 5 fixtures + 7 scenarios × 100 bp (true rules) + 600 random "
            "(`gen_cases.random_cases(55, 600)`). Random = `gen_cases.random_cases(seed, n)`. "
            "Adversarial = `gen_cases.adversarial_cases(seed, n)` (planted lifted levels, "
            "shared-factor runs, tiers/tokens inside runs, tight caps, mid-month/EOM). "
            "\"ref p50 / max s\" is reference + legacy + witness checks per case.", "",
            "## Structural sweep (no ledger)", "",
            "For random staircase rules (3–4 segments, tiers, tokens) and S near the floors' "
            "sum (each draw also repeated with its tiers removed), every reference family "
            "vector is tested for a legacy vector of the same k "
            "with pointwise smaller or equal prefix sums. No such vector means a ledger exists "
            "(caps equal to its prefix sums) where legacy misses; one is built and verified "
            "end to end for the smallest non-dominated vector of each draw.", "",
            "| size | draws | family vectors | draws with a miss | vectors not dominated | "
            "witnesses verified (false infeasible) | engine misses on them |",
            "|---|---|---|---|---|---|---|"]
    for size, r in summary["structural"].items():
        out.append(f"| {size} | {r['draws']} | {r['family_vectors']} | {r['draws_with_miss']} | "
                   f"{r['vectors_not_dominated']} | {r['witness_verified']} | "
                   f"{r['engine_misses']} |")
    out += ["", "## Misses by kind, set and structure", "", "| kind / set / structure | count |",
            "|---|---|"]
    for k, v in sorted(summary["by_structure"].items()):
        out.append(f"| {k} | {v} |")
    out += ["", "| kind / witness run lengths | count |", "|---|---|"]
    for k, v in sorted(summary["by_runs"].items(), key=lambda kv: -kv[1])[:25]:
        out.append(f"| {k} | {v} |")
    out += ["", "| kind / witness has a lifted early level (above the legacy pin) / legacy "
            "family empty for this S | count |",
            "|---|---|"]
    for k, v in sorted(summary["mechanism"].items()):
        out.append(f"| {k} | {v} |")
    if summary["bugs"]:
        out += ["", "## Consistency bugs", ""] + [f"- {b}" for b in summary["bugs"][:20]]
    if summary["engine_misses"]:
        out += ["", "## Engine misses", ""] + [f"- {b}" for b in summary["engine_misses"][:20]]
    out += ["", "## Run time (s)", "", "| step | seconds |", "|---|---|"]
    for k, v in summary["timing_secs"].items():
        out.append(f"| {k} | {v} |")
    for kind, title in FULL_TITLES.items():
        out += ["", f"## {title}: smallest shrunk witnesses", ""]
        rs = shrunk.get(kind, [])
        out.append("\n".join(f"{_fmt_full(r)}\n  - (set {r['set']}, shrunk from `{r['from']}`)"
                             for r in rs) if rs else "None found.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cases", type=int, default=5000)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", type=Path, default=None, help="output dir (default docs/eval/...)")
    p.add_argument("--cap-draws", type=int, default=4000, help="prefix-cap model draws per size")
    p.add_argument("--mode", choices=("small", "full"), default="small")
    p.add_argument("--random", type=int, default=20000, help="full: seeded random cases")
    p.add_argument("--adversarial", type=int, default=20000, help="full: adversarial cases")
    p.add_argument("--structural", type=int, default=2000, help="full: full-size rule draws")
    p.add_argument("--structural-small", type=int, default=5000, help="full: small rule draws")
    p.add_argument("--ref-check", default="1:5000,2:10000", help="full: seed:n small sets")
    p.add_argument("--prune-check", type=int, default=300, help="full: unpruned cases per set")
    p.add_argument("--shrink", type=int, default=12, help="full: misses shrunk per kind")
    p.add_argument("--shrink-budget", type=int, default=300)
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)
    if args.mode == "full":
        return run_full(args)
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
