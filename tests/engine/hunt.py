"""Full-size legacy-vs-reference comparison, structural dominance check, witness shrinking.

The Phase 57 hunt asks whether the frozen take-home family (``feasibility/legacy.py``)
ever answers worse than the exact reference (``tests/engine/reference.py``) on full-size
cases. This module holds the per-case logic so the script and the tests share it:

- ``compare_case(case)``: legacy and reference on one case dict, the miss kinds
  (``false_infeasible``, ``worse_phi``, ``rescue_over_lump``, ``rescue_over_increment``)
  and consistency bugs. Every reference witness is re-checked independently of the
  prefix-cap shortcut: ``oracle.is_legal``, an ``oracle.Instance`` ledger replay (Φ, or
  feasibility at exactly L*/X* and not one cent below), and the validator.
- ``structural_misses(rules, S, k)``: reference family vectors that no legacy vector of
  the same k dominates (pointwise smaller or equal prefix sums). By the monotonicity in
  ``reference.py`` this is the cap-free form of the question: with no such vector, the
  legacy family is exact for every ledger on these rules and S.
- ``shrink(case, kind)``: greedy smaller witnesses that still miss the same way.

Returns plain dicts (picklable for ``multiprocessing``). Never used by ``app/``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from itertools import accumulate
from typing import Any

from app.adapter.validator import validate
from feasibility.engine import Result
from feasibility.legacy import legacy_candidates, legacy_evaluate
from feasibility.models import CreditorRules
from feasibility.problem import Problem, build_problem
from feasibility.shapes import compute_floors
from tests.engine import gen_cases, oracle, reference

KINDS = ("false_infeasible", "worse_phi", "rescue_over_lump", "rescue_over_increment")
UNFIXABLE_PREFIX = "no additional funding"


def _problem(case: dict[str, Any]) -> tuple[Any, Any, CreditorRules, Problem]:
    client, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    return client, offer, rules, problem


def structure_of(rules: dict[str, Any]) -> str:
    """Rule-driven structure label for reporting."""
    if rules["even_pays"]:
        return "even"
    return "balloon" if rules["is_ballooning_allowed"] else "staircase"


def _legacy_phi(problem: Problem, result: Result) -> tuple[int, ...] | None:
    if result.schedule is None:
        return None
    fee_on: dict[date, int] = {r.date: r.program_fee_cents for r in result.schedule}
    out, run = [], 0
    for t in problem.cadence:
        run += fee_on.get(t, 0)
        out.append(run)
    return tuple(out)


def _legacy_rescue(result: Result) -> tuple[int | None, int | None]:
    af = result.additional_funds
    assert af is not None
    lump = None if af.lump_sum.reason.startswith(UNFIXABLE_PREFIX) else af.lump_sum.amount_cents
    incr = af.monthly_increment
    return lump, (None if incr.reason.startswith(UNFIXABLE_PREFIX) else incr.amount_cents)


def runs_of(vec: tuple[int, ...] | list[int]) -> list[int]:
    """Maximal run lengths of equal payments."""
    out: list[int] = []
    for i, p in enumerate(vec):
        if i and p == vec[i - 1]:
            out[-1] += 1
        else:
            out.append(1)
    return out


def lifted_levels(vec: tuple[int, ...] | list[int], rules: CreditorRules) -> list[int]:
    """1-based indices l <= m-2 whose level is above its pinned lower bound (legacy pins)."""
    runs = runs_of(vec)
    f = reference.floors_with_tokens(rules, len(vec))
    out, pos, prev = [], 0, 0
    for li, c in enumerate(runs[:-2]):
        pos += c
        v = vec[pos - 1]
        lo = f[pos] if li == 0 else max(f[pos], prev + 1)
        if v > lo:
            out.append(li + 1)
        prev = v
    return out


def _check_rescue(
    inst: oracle.Instance, rules: CreditorRules, S: int, k_cap: int,
    vec: tuple[int, ...] | None, amount: int | None, which: str,
) -> bool:
    """Independent replay: ``vec`` is legal, feasible at ``amount`` and not one cent below."""
    if amount is None:
        return vec is None
    if vec is None or not oracle.is_legal(vec, S, k_cap, rules):
        return False

    def ok(a: int) -> bool:
        L, X = (a, 0) if which == "L" else (0, a)
        return inst.phi(inst.replay(vec, L, X)) is not None

    return ok(amount) and (amount == 0 or not ok(amount - 1))


def compare_case(case: dict[str, Any], *, prune: bool = True) -> dict[str, Any]:
    """Legacy vs reference on one case: kinds, bugs and a compact record."""
    client, offer, rules, problem = _problem(case)
    S = problem.offer_total(offer.settlement_bp)
    F = problem.program_fee_cents
    leg = legacy_evaluate(problem, offer.settlement_bp)
    ref = reference.solve(problem, S, prune=prune)
    fpd = problem.first_payment_date
    inst = oracle.Instance(client, rules, fpd, S, F)
    kinds: list[str] = []
    bugs: list[str] = []
    rec: dict[str, Any] = {
        "id": case["id"],
        "S": S,
        "F": F,
        "k_max": problem.k_max,
        "M": problem.M,
        "structure": structure_of(case["rules"]),
        "segments": rules.max_segments,
        "ledger": len(case["client"]["ledger"]),
        "ref_feasible": ref.feasible,
        "legacy_feasible": leg.feasible,
        "scored": ref.scored,
    }
    if ref.feasible:
        assert ref.best is not None and ref.phi is not None
        rows = inst.rows(ref.best, ref.phi)
        if not oracle.is_legal(ref.best, S, problem.k_max, rules):
            bugs.append("ref_illegal")
        if inst.phi(inst.replay(ref.best)) != ref.phi:
            bugs.append("ref_phi_replay")
        if validate(rows, client, S, F, rules, fpd):
            bugs.append("ref_validator")
        rec["ref_vec"] = list(ref.best)
        rec["ref_phi"] = list(ref.phi)
    else:
        if not _check_rescue(inst, rules, S, problem.k_max, ref.lump_vec, ref.lump_cents, "L"):
            bugs.append("ref_lump_witness")
        if not _check_rescue(
            inst, rules, S, problem.k_max, ref.increment_vec, ref.increment_cents, "X"
        ):
            bugs.append("ref_increment_witness")
        rec["ref_lump"], rec["ref_increment"] = ref.lump_cents, ref.increment_cents
        rec["ref_lump_vec"] = list(ref.lump_vec) if ref.lump_vec else None
        rec["ref_increment_vec"] = list(ref.increment_vec) if ref.increment_vec else None
    if leg.feasible:
        assert leg.schedule is not None
        vec = tuple(r.creditor_payment_cents for r in leg.schedule if r.creditor_payment_cents)
        phi = _legacy_phi(problem, leg)
        rec["legacy_vec"], rec["legacy_phi"] = list(vec), list(phi or ())
        if not ref.feasible:
            bugs.append("legacy_feasible_ref_not")
        else:
            assert phi is not None and ref.phi is not None
            if phi < ref.phi:
                kinds.append("worse_phi")
            elif phi > ref.phi:
                bugs.append("legacy_phi_above_ref")
    elif ref.feasible:
        kinds.append("false_infeasible")
        rec["legacy_lump"], rec["legacy_increment"] = _legacy_rescue(leg)
    else:
        lump, incr = _legacy_rescue(leg)
        rec["legacy_lump"], rec["legacy_increment"] = lump, incr
        for kind, lv, rv in (
            ("rescue_over_lump", lump, ref.lump_cents),
            ("rescue_over_increment", incr, ref.increment_cents),
        ):
            if rv is not None and (lv is None or lv > rv):
                kinds.append(kind)
            if lv is not None and (rv is None or lv < rv):
                bugs.append(kind.replace("over", "under"))
    witness = rec.get("ref_vec") or rec.get("ref_lump_vec") or rec.get("ref_increment_vec")
    if witness:
        rec["runs"] = runs_of(witness)
        rec["lifted"] = lifted_levels(witness, rules)
    # Legacy has no statically legal vector at all (its rescue says "unfixable").
    rec["legacy_empty"] = not leg.feasible and rec.get("legacy_lump") is None
    rec["kinds"], rec["bugs"] = kinds, bugs
    return rec


# --- structural (cap-free) dominance ------------------------------------------------


def structural_misses(rules: CreditorRules, S: int, k: int) -> tuple[int, list[tuple[int, ...]]]:
    """(family size, family vectors of length k that no legacy length-k vector dominates)."""
    fam = list(reference.Reference.structural(rules, S, k).family(k))
    leg = [c.payments for c in legacy_candidates(S, k, compute_floors(rules, k), rules) if c.k == k]
    leg_p = [list(accumulate(v))[:-1] for v in leg]
    # Keep only Pareto-minimal legacy prefixes: a dominated one never decides.
    leg_p.sort()
    minimal: list[list[int]] = []
    for p in leg_p:
        if not any(all(a <= b for a, b in zip(q, p)) for q in minimal):
            minimal.append(p)
    out = []
    for v in fam:
        P = list(accumulate(v))[:-1]
        if not any(all(a <= b for a, b in zip(q, P)) for q in minimal):
            out.append(v)
    return len(fam), out


def structural_witness_case(
    case_id: str, rules: dict[str, Any], vec: tuple[int, ...], *, t1: date, F: int
) -> dict[str, Any]:
    """A ledger whose window caps are exactly ``vec``'s prefix sums, ``max_payments = k``.

    Every shorter k' cannot pay S by window k' (cap < S there), so a non-dominated ``vec``
    makes the legacy family infeasible while ``vec`` itself is feasible.
    """
    k = len(vec)
    r = {**rules, "max_payments": k, "max_terms": k}
    return gen_cases.planted_case(
        case_id, list(vec), r, t1=t1, F=F, window_slack=[0] * (k - 1), tail_extra=[F]
    )


# --- shrinking -------------------------------------------------------------------


def _scale(case: dict[str, Any], num: int, den: int) -> dict[str, Any]:
    c = {**case, "client": dict(case["client"]), "rules": dict(case["rules"])}
    c.pop("planted", None)
    s = lambda x: max(1, x * num // den)  # noqa: E731
    c["client"]["ledger"] = [[d, s(a), t] for d, a, t in case["client"]["ledger"]]
    c["client"]["current_balance_cents"] = case["client"]["current_balance_cents"] * num // den
    c["client"]["draft_amount_cents"] = s(case["client"]["draft_amount_cents"])
    r = c["rules"]
    r["min_payment_cents"] = s(r["min_payment_cents"])
    r["min_payment_tiers"] = [[a, s(b)] for a, b in r["min_payment_tiers"]]
    r["bank_fee_cents"] = r["bank_fee_cents"] * num // den
    c["creditor_balance_cents"] = s(case["creditor_balance_cents"])
    c["original_balance_cents"] = s(case["original_balance_cents"])
    return c


def _shrink_moves(case: dict[str, Any]) -> list[dict[str, Any]]:
    moves: list[dict[str, Any]] = []
    r = case["rules"]
    if r["max_payments"] > 1:
        moves.append({**case, "rules": {**r, "max_payments": r["max_payments"] - 1,
                                        "max_terms": min(r["max_terms"], r["max_payments"] - 1)}})
    led = case["client"]["ledger"]
    credits = [i for i, e in enumerate(led) if e[2] == "credit"]
    if len(credits) > 1:
        rest = [e for i, e in enumerate(led) if i != credits[-1]]
        last = max(e[0] for e in rest if e[2] == "credit")
        moves.append({**case, "client": {**case["client"], "ledger": rest,
                                         "last_draft_date": last}})
    for i, e in enumerate(led):
        if e[2] == "debit":
            moves.append({**case, "client": {**case["client"],
                                             "ledger": led[:i] + led[i + 1:]}})
    for i in range(len(r["min_payment_tiers"])):
        tiers = r["min_payment_tiers"][:i] + r["min_payment_tiers"][i + 1:]
        moves.append({**case, "rules": {**r, "min_payment_tiers": tiers}})
    if case["client"]["current_balance_cents"]:
        moves.append({**case, "client": {**case["client"], "current_balance_cents": 0}})
    for num, den in ((1, 2), (3, 4), (9, 10), (99, 100)):
        moves.append(_scale(case, num, den))
    return moves


def size_key(rec: dict[str, Any]) -> tuple[int, ...]:
    """Smaller is simpler: payments, then money, then horizon and ledger length."""
    return (rec["k_max"], rec["S"], rec["M"], rec["ledger"])


def shrink(
    case: dict[str, Any], kind: str, *, budget: int = 300,
    run: Callable[[dict[str, Any]], dict[str, Any]] = compare_case,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Greedy shrink while ``kind`` still misses with no bug; returns (case, record)."""
    best_case, best = case, run(case)
    tries = 0
    improved = True
    while improved and tries < budget:
        improved = False
        for cand in _shrink_moves(best_case):
            tries += 1
            try:
                rec = run(cand)
            except (ValueError, AssertionError):
                continue
            if kind in rec["kinds"] and not rec["bugs"] and size_key(rec) < size_key(best):
                best_case, best, improved = cand, rec, True
                break
            if tries >= budget:
                break
    return best_case, best
