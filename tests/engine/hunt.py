"""Per-case checks of the engine and the exact reference on full-size cases.

Phase 57 used this module to hunt for misses of the take-home candidate family; that
family and the gap-report script were removed in Phase 61 (the frozen reports stay in
``docs/eval/engine_gap_*``). What remains is shared by ``test_search.py`` and
``test_reference.py``:

- ``compare_engine(case)``: the engine (``feasibility.engine.evaluate``, the exact
  search) against ``tests/engine/reference.py``: miss kinds (``false_infeasible``,
  ``worse_phi``, ``rescue_over_lump``, ``rescue_over_increment``; must be empty),
  consistency bugs, the validator on its schedule, and optionally strict minimality of
  its rescue amounts.
- ``check_reference(case)``: the reference's own witnesses re-checked independently of
  the prefix-cap shortcut: ``oracle.is_legal``, an ``oracle.Instance`` ledger replay (Φ,
  or feasibility at exactly L*/X* and not one cent below), and the validator.
- ``structural_witness_case``: a ledger whose window caps are a given vector's prefix
  sums (how the Phase 57 structural witnesses were built).

Returns plain dicts. Never used by ``app/``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.adapter.validator import validate
from feasibility.engine import Result, evaluate
from feasibility.models import CreditorRules
from feasibility.problem import Problem, build_problem
from tests.engine import gen_cases, oracle, reference

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


def phi_of_result(problem: Problem, result: Result) -> tuple[int, ...] | None:
    """Cumulative program fee by each cadence date of a feasible result (None if infeasible)."""
    if result.schedule is None:
        return None
    fee_on: dict[date, int] = {r.date: r.program_fee_cents for r in result.schedule}
    out, run = [], 0
    for t in problem.cadence:
        run += fee_on.get(t, 0)
        out.append(run)
    return tuple(out)


def rescue_of(result: Result) -> tuple[int | None, int | None]:
    """(lump, increment) of an infeasible result; None where it says "unfixable"."""
    af = result.additional_funds
    assert af is not None
    lump = None if af.lump_sum.reason.startswith(UNFIXABLE_PREFIX) else af.lump_sum.amount_cents
    incr = af.monthly_increment
    return lump, (None if incr.reason.startswith(UNFIXABLE_PREFIX) else incr.amount_cents)


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


def _kinds_vs_ref(
    problem: Problem, result: Result, ref: reference.RefResult, rec: dict[str, Any], tag: str
) -> tuple[list[str], list[str]]:
    """Miss kinds and consistency bugs of one engine ``Result`` against the reference."""
    kinds: list[str] = []
    bugs: list[str] = []
    if result.feasible:
        phi = phi_of_result(problem, result)
        if not ref.feasible:
            bugs.append(f"{tag}_feasible_ref_not")
        else:
            assert phi is not None and ref.phi is not None
            if phi < ref.phi:
                kinds.append("worse_phi")
            elif phi > ref.phi:
                bugs.append(f"{tag}_phi_above_ref")
    elif ref.feasible:
        kinds.append("false_infeasible")
        rec[f"{tag}_lump"], rec[f"{tag}_increment"] = rescue_of(result)
    else:
        lump, incr = rescue_of(result)
        rec[f"{tag}_lump"], rec[f"{tag}_increment"] = lump, incr
        for kind, lv, rv in (
            ("rescue_over_lump", lump, ref.lump_cents),
            ("rescue_over_increment", incr, ref.increment_cents),
        ):
            if rv is not None and (lv is None or lv > rv):
                kinds.append(kind)
            if lv is not None and (rv is None or lv < rv):
                bugs.append(f"{tag}_" + kind.replace("over", "under"))
    return kinds, bugs


def compare_engine(
    case: dict[str, Any], ref: reference.RefResult | None = None, *, minimal: bool = False
) -> dict[str, Any]:
    """Engine (exact search) vs reference on one case: ``kinds`` must be empty.

    Also validates the engine's schedule, and with ``minimal=True`` replays its L*/X*
    (feasible at the amount, not one cent below) on the reference's witness vectors.
    """
    client, offer, rules, problem = _problem(case)
    S = problem.offer_total(offer.settlement_bp)
    F = problem.program_fee_cents
    res = evaluate(problem, offer.settlement_bp)
    if ref is None:
        ref = reference.solve(problem, S)
    rec: dict[str, Any] = {"id": case["id"], "feasible": res.feasible,
                           "ref_feasible": ref.feasible}
    kinds, bugs = _kinds_vs_ref(problem, res, ref, rec, "engine")
    if res.schedule is not None:
        fpd = problem.first_payment_date
        if validate(res.schedule, client, S, F, rules, fpd):
            bugs.append("engine_validator")
    elif minimal and not kinds:
        inst = oracle.Instance(client, rules, problem.first_payment_date, S, F)
        for which, vec, amount in (
            ("L", ref.lump_vec, rec.get("engine_lump")),
            ("X", ref.increment_vec, rec.get("engine_increment")),
        ):
            if not _check_rescue(inst, rules, S, problem.k_max, vec, amount, which):
                bugs.append(f"engine_{which}_not_minimal")
    rec["kinds"], rec["bugs"] = kinds, bugs
    return rec


def check_reference(case: dict[str, Any], *, prune: bool = True) -> dict[str, Any]:
    """Reference answer on one case with its witnesses re-checked; ``bugs`` must be empty."""
    client, offer, rules, problem = _problem(case)
    S = problem.offer_total(offer.settlement_bp)
    F = problem.program_fee_cents
    ref = reference.solve(problem, S, prune=prune)
    fpd = problem.first_payment_date
    inst = oracle.Instance(client, rules, fpd, S, F)
    bugs: list[str] = []
    rec: dict[str, Any] = {"id": case["id"], "ref_feasible": ref.feasible}
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
    rec["bugs"] = bugs
    return rec


# --- structural witnesses ------------------------------------------------------


def structural_witness_case(
    case_id: str, rules: dict[str, Any], vec: tuple[int, ...], *, t1: date, F: int
) -> dict[str, Any]:
    """A ledger whose window caps are exactly ``vec``'s prefix sums, ``max_payments = k``.

    Every shorter k' cannot pay S by window k' (cap < S there), so a non-dominated ``vec``
    makes any family without a dominating vector infeasible while ``vec`` itself is feasible.
    """
    k = len(vec)
    r = {**rules, "max_payments": k, "max_terms": k}
    return gen_cases.planted_case(
        case_id, list(vec), r, t1=t1, F=F, window_slack=[0] * (k - 1), tail_extra=[F]
    )
