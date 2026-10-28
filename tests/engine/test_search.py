"""Exact search (``feasibility/search.py``) against the reference and the oracle.

- Engine vs ``tests/engine/reference.py`` (exact, independent): feasibility, Φ*, L*, X*
  equal, every schedule validator-clean. Fast: every Phase 57 witness
  (``golden/witnesses_57.jsonl``: 112 structural, 18 adversarial cases) plus the golden
  inputs and 600 adversarial cases. Slow: the Phase 57 sets (20,000 random, 20,000
  adversarial, seed 57).
- Engine vs ``tests/engine/oracle.py`` (brute force) on small cases: feasibility and Φ*
  on all, L* and X* on the infeasible ones (≥ 5,000 under ``slow``).
- Strict minimality at full size, independent of the search: topping the client up by
  L* (or every draft by X*) makes the offer feasible, one cent less does not.
- ``Search.key4`` equals ``scoring.score_key`` on the vectors of the pinned stage.

The take-home family comparison (never worse, identical where it was exact) ran until
Phase 61 against ``feasibility/legacy.py``; its results are frozen in
``docs/eval/engine_gap_20261027_full/`` and the golden corpus (``test_golden.py``)
still pins the take-home outputs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from itertools import accumulate
from pathlib import Path

import pytest

from feasibility.engine import evaluate
from feasibility.models import Client, LedgerEntry
from feasibility.problem import Problem, build_problem
from feasibility.scoring import evaluate_candidate, score_key
from feasibility.search import Search
from feasibility.shapes import Candidate, label_shape
from tests.engine import gen_cases, hunt, oracle
from tests.engine.golden.build_golden import GOLDEN, expand

WITNESSES = [
    json.loads(line)
    for line in (Path(__file__).parent / "golden" / "witnesses_57.jsonl").read_text().splitlines()
]


def _golden_cases() -> list[dict]:
    out = []
    for line in GOLDEN.read_text().splitlines():
        rec = json.loads(line)
        for c in expand(rec):
            out.append({**c, "id": f"{c['id']}@{c['settlement_bp']}"} if "bps" in rec else c)
    return out


def _problem(case: dict, client: Client | None = None) -> tuple[Problem, int]:
    c, offer, rules = gen_cases.case_to_objects(case)
    problem = build_problem(
        client or c,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    return problem, offer.settlement_bp


def _bad(cases: list[dict], *, minimal: bool = False) -> list[tuple]:
    out = []
    for case in cases:
        rec = hunt.compare_engine(case, minimal=minimal)
        if rec["kinds"] or rec["bugs"]:
            out.append((case["id"], rec["kinds"], rec["bugs"]))
    return out


# --- engine vs the exact reference ---------------------------------------------


def test_witness_file_covers_phase57() -> None:
    sets = [w["set"] for w in WITNESSES]
    assert sets.count("structural") == 112
    assert sum(len(w["kinds"]) for w in WITNESSES if w["set"] == "adversarial") == 25


def test_engine_exact_on_every_phase57_witness() -> None:
    """Each witness made the take-home family miss (``kinds``); the engine matches the
    reference on all of them."""
    assert all(w["kinds"] for w in WITNESSES)
    assert _bad([w["case"] for w in WITNESSES], minimal=True) == []


def test_engine_exact_fast_sets() -> None:
    """Golden inputs (incl. 7 scenarios x 100 bp) and 600 adversarial cases."""
    cases = _golden_cases() + gen_cases.adversarial_cases(57, 600)
    assert len(cases) + len(WITNESSES) >= 2000
    assert _bad(cases) == []


@pytest.mark.slow
@pytest.mark.parametrize("which", ["random", "adversarial"])
def test_engine_exact_phase57_sets(which: str) -> None:
    cases = (
        gen_cases.random_cases(57, 20000, prefix="f")
        if which == "random"
        else gen_cases.adversarial_cases(57, 20000)
    )
    assert _bad(cases) == []


# --- engine vs the brute-force oracle (small) --------------------------------------


def _vs_oracle(case: dict) -> tuple[bool, bool]:
    """(engine agrees with the oracle, oracle feasible) on one small case."""
    problem, bp = _problem(case)
    S = problem.offer_total(bp)
    o = oracle.solve(problem.client, problem.rules, problem.first_payment_date, S,
                     problem.program_fee_cents)
    res = evaluate(problem, bp)
    if res.feasible != o.feasible:
        return False, o.feasible
    if o.feasible:
        return hunt.phi_of_result(problem, res) == o.phi, True
    lump, incr = hunt.rescue_of(res)
    return (lump, incr) == (o.lump_cents, o.increment_cents), False


def _oracle_sweep(seed: int, n: int) -> tuple[list[str], int]:
    bad, infeasible = [], 0
    for case in gen_cases.random_cases(seed, n, small=True, prefix="g"):
        try:
            ok, feas = _vs_oracle(case)
        except oracle.OracleTooBig:
            continue
        infeasible += not feas
        if not ok:
            bad.append(case["id"])
    return bad, infeasible


def test_engine_matches_oracle_small_fast() -> None:
    bad, infeasible = _oracle_sweep(3, 800)
    assert bad == [] and infeasible > 300


@pytest.mark.slow
def test_engine_matches_oracle_small_phase55_sets() -> None:
    """Seeds 1 and 2 (15,000 cases): ≥ 5,000 infeasible checked for L* and X*."""
    total = 0
    for seed, n in ((1, 5000), (2, 10000)):
        bad, infeasible = _oracle_sweep(seed, n)
        assert bad == []
        total += infeasible
    assert total >= 5000


# --- strict minimality at full size, through a topped-up client --------------------


def _topped_up(client: Client, lump: int = 0, per_draft: int = 0) -> Client:
    ledger = [
        replace(e, amount_cents=e.amount_cents + per_draft)
        if e.type == "credit" and e.date > client.as_of_date
        else e
        for e in client.ledger
    ]
    return replace(
        client, current_balance_cents=client.current_balance_cents + lump,
        ledger=[LedgerEntry(e.date, e.amount_cents, e.type) for e in ledger],
    )


def _minimality(case: dict) -> list[str]:
    """L*/X* reported for an infeasible case are exactly the smallest amounts that work."""
    problem, bp = _problem(case)
    res = evaluate(problem, bp)
    if res.feasible:
        return []
    lump, incr = hunt.rescue_of(res)
    errs = []
    for name, amount, top in (
        ("L", lump, lambda a: _topped_up(problem.client, lump=a)),
        ("X", incr, lambda a: _topped_up(problem.client, per_draft=a)),
    ):
        if amount is None:
            continue
        if not evaluate(_problem(case, top(amount))[0], bp).feasible:
            errs.append(f"{case['id']}: {name}*={amount} not enough")
        if amount > 0 and evaluate(_problem(case, top(amount - 1))[0], bp).feasible:
            errs.append(f"{case['id']}: {name}*-1 already feasible")
    return errs


def test_rescue_strictly_minimal_on_fixtures_and_witnesses() -> None:
    cases = [c for c in _golden_cases() if c["id"].startswith(("fx_", "scn_"))]
    cases += [w["case"] for w in WITNESSES]
    errs = [e for c in cases for e in _minimality(c)]
    assert errs == []


# --- key4 against the full score_key -------------------------------------------


def test_key4_matches_score_key() -> None:
    for case in gen_cases.random_cases(55, 120):
        problem, bp = _problem(case)
        S = problem.offer_total(bp)
        search = Search(problem, S)
        vecs = [v for k in range(1, problem.k_max + 1) for v in search.pinned(k, lambda _: False)]
        for vec in vecs:
            cand = Candidate(k=len(vec), payments=vec, label=label_shape(vec, problem.rules))
            got = search.key4(list(accumulate(cand.payments)), cand.k, 0)
            scored = evaluate_candidate(
                problem.client, cand, problem.cadence, problem.rules.bank_fee_cents,
                problem.program_fee_cents, 0, problem.base,
            )
            if scored is None:
                assert got is None
                continue
            want = score_key(cand, scored[0], scored[1], problem.M)[:4]
            assert got == want, (case["id"], cand)
