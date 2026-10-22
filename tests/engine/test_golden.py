"""Golden corpus: the engine must reproduce every frozen output byte for byte.

``golden/golden.jsonl`` was written by the pre-Phase-55 engine (5 engine fixtures,
7 scenarios × 100 bp, 600 seeded random cases; see ``golden/build_golden.py``).
Any diff here is a behaviour change: either a bug, or an intended change that must
regenerate the file and record why in docs/history/PROGRESS.md.
"""

from __future__ import annotations

import json

import pytest

from feasibility.engine import evaluate
from feasibility.problem import build_problem
from tests.engine import gen_cases
from tests.engine.golden.build_golden import GOLDEN, compact, expand, run_case

RECORDS = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line]


def test_corpus_shape() -> None:
    ids = [r["id"] for r in RECORDS]
    assert len(ids) == len(set(ids)) == 5 + 7 + 600
    assert sum(len(r["outs"]) for r in RECORDS if "outs" in r) == 700
    assert GOLDEN.stat().st_size < 3_000_000


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_golden_output_identical(record: dict) -> None:
    expected = record["outs"] if "bps" in record else [record["out"]]
    got = [run_case(c) for c in expand(record)]
    assert got == expected


@pytest.mark.parametrize(
    "record", [r for r in RECORDS if "bps" in r], ids=lambda r: r["id"]
)
def test_shared_problem_matches_per_offer(record: dict) -> None:
    """One ``Problem`` reused across a 100-bp scan gives the per-offer outputs."""
    client, offer, rules = gen_cases.case_to_objects({**record["case"], "settlement_bp": 100})
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    got = [compact(evaluate(problem, bp).to_dict()) for bp in record["bps"]]
    assert got == record["outs"]


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_frozen_legacy_generator_matches_golden(record: dict) -> None:
    """``feasibility.legacy`` must keep reproducing the pre-v2 outputs (gap-report baseline)."""
    from feasibility.legacy import legacy_evaluate

    expected = record["outs"] if "bps" in record else [record["out"]]
    got = []
    for case in expand(record):
        client, offer, rules = gen_cases.case_to_objects(case)
        problem = build_problem(
            client,
            rules,
            offer.first_payment_date,
            creditor_balance_cents=offer.creditor_balance_cents,
            original_balance_cents=offer.original_balance_cents,
        )
        got.append(compact(legacy_evaluate(problem, offer.settlement_bp).to_dict()))
    assert got == expected
