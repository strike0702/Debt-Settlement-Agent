"""Build ``golden.jsonl``: frozen engine outputs that ``test_golden.py`` must reproduce.

Three groups, every input embedded so the corpus survives fixture edits:

- the 5 ``fixtures/engine`` cases at their own settlement %;
- the 7 ``fixtures/scenarios`` with their true ``sim.json`` rules × 100 bp
  (100..10000), one line per scenario with 100 outputs;
- 600 seeded random cases from ``gen_cases.random_cases(55, 600)``.

Outputs are ``Result.to_dict()`` with schedule rows packed as
``[date, creditor, program_fee, bank_fee, balance]`` (``compact``). Regenerate only
for an intended behaviour change, with a PROGRESS note:
``uv run python -m tests.engine.golden.build_golden``.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.engine import gen_cases

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).resolve().parent / "golden.jsonl"
FIXTURE_CASES = ("balloon_ok", "even_ok", "gap_curve", "rescue_gap", "tier_ok")
SCENARIOS = (
    "balloon_structure",
    "counter_ladder",
    "easy_deal",
    "haggling_rep",
    "late_start_date",
    "no_space",
    "rescue_escalate",
)
SCAN_BPS = tuple(range(100, 10001, 100))
RANDOM_SEED = 55
RANDOM_COUNT = 600


def to_bp(fraction: Any) -> int:
    """Exact fraction -> bp; raises if the value is not a whole basis point."""
    scaled = Decimal(str(fraction)) * 10000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{fraction!r} is not a whole basis point")
    return int(scaled)


def _rules_from_json(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_terms": int(raw["max_terms"]),
        "max_payments": int(raw["max_payments"]),
        "min_payment_cents": int(raw["min_payment_cents"]),
        "max_token_pays": int(raw["max_token_pays"]),
        "min_payment_tiers": [[int(a), int(b)] for a, b in raw.get("min_payment_tiers", [])],
        "even_pays": bool(raw.get("even_pays", False)),
        "is_ballooning_allowed": bool(raw.get("is_ballooning_allowed", False)),
        "max_segments": int(raw.get("max_segments", 4)),
        "bank_fee_cents": int(raw["bank_fee_cents"]),
        "program_fee_bp": to_bp(raw["program_fee_pct"]),
    }


def fixture_case(name: str) -> gen_cases.Case:
    """One ``fixtures/engine`` case as a case dict."""
    folder = ROOT / "fixtures" / "engine" / name
    client = json.loads((folder / "client.json").read_text())
    offer = json.loads((folder / "offer.json").read_text())
    rules = json.loads((folder / "creditor_rules.json").read_text())
    balance = offer.get("creditor_balance_cents", offer.get("current_balance_cents"))
    return {
        "id": f"fx_{name}",
        "client": {
            **{k: client[k] for k in client if k != "ledger"},
            "ledger": [[e["date"], e["amount_cents"], e["type"]] for e in client["ledger"]],
        },
        "creditor": offer["creditor"],
        "creditor_balance_cents": int(balance),
        "original_balance_cents": int(offer["original_balance_cents"]),
        "settlement_bp": to_bp(offer["settlement_pct"]),
        "first_payment_date": offer.get("first_payment_date"),
        "rules": _rules_from_json(rules),
    }


def scenario_case(name: str) -> gen_cases.Case:
    """One ``fixtures/scenarios`` case with its true rules (``settlement_bp`` set per scan)."""
    from app.domain.scenario import load_scenario
    from feasibility.models import add_months, end_of_month

    folder = ROOT / "fixtures" / "scenarios" / name
    call = load_scenario(folder)
    truth = json.loads((folder / "sim.json").read_text())["rules"]
    spec = str(truth["first_payment_date"])
    if spec == "default":
        fpd = end_of_month(call.client.first_draft_date)
    elif spec == "after_last_draft":
        fpd = end_of_month(add_months(call.client.last_draft_date, 1))
    else:
        fpd = date.fromisoformat(spec)
    structure = truth["payment_structure"]
    return {
        "id": f"scn_{name}",
        "client": gen_cases.client_to_dict(call.client),
        "creditor": call.creditor,
        "creditor_balance_cents": call.creditor_balance_cents,
        "original_balance_cents": call.original_balance_cents,
        "settlement_bp": None,
        "first_payment_date": fpd.isoformat(),
        "rules": {
            "max_terms": int(truth["max_payments"]),
            "max_payments": int(truth["max_payments"]),
            "min_payment_cents": int(truth["min_payment_cents"]),
            "max_token_pays": int(truth["max_token_pays"]),
            "min_payment_tiers": [[int(a), int(b)] for a, b in truth["min_payment_tiers"]],
            "even_pays": structure == "even",
            "is_ballooning_allowed": structure == "balloon",
            "max_segments": int(truth["max_segments"]),
            "bank_fee_cents": call.bank_fee_cents,
            "program_fee_bp": to_bp(call.program_fee_pct),
        },
    }


def compact(out: dict[str, Any]) -> dict[str, Any]:
    """Lossless packing of ``Result.to_dict()`` (schedule rows -> lists)."""
    packed = dict(out)
    if out["schedule"] is not None:
        packed["schedule"] = [
            [
                r["date"],
                r["creditor_payment_cents"],
                r["program_fee_cents"],
                r["bank_fee_cents"],
                r["balance_cents"],
            ]
            for r in out["schedule"]
        ]
    return packed


def run_case(case: gen_cases.Case) -> dict[str, Any]:
    """Evaluate one case through the public ``evaluate_offer`` and pack the output."""
    from feasibility.engine import evaluate_offer

    client, offer, rules = gen_cases.case_to_objects(case)
    return compact(evaluate_offer(client, offer, rules).to_dict())


def corpus_inputs() -> list[dict[str, Any]]:
    """Every golden record's input part, in file order (outputs not filled)."""
    records: list[dict[str, Any]] = [
        {"id": c["id"], "case": c} for c in map(fixture_case, FIXTURE_CASES)
    ]
    for name in SCENARIOS:
        c = scenario_case(name)
        records.append({"id": c["id"], "case": c, "bps": list(SCAN_BPS)})
    for c in gen_cases.random_cases(RANDOM_SEED, RANDOM_COUNT):
        records.append({"id": c["id"], "case": c})
    return records


def expand(record: dict[str, Any]) -> list[gen_cases.Case]:
    """The concrete cases one record stands for (100 for a scenario scan)."""
    case = record["case"]
    if "bps" not in record:
        return [case]
    return [{**case, "settlement_bp": bp} for bp in record["bps"]]


def build() -> None:
    """Evaluate every record with the current engine and write ``golden.jsonl``."""
    lines: list[str] = []
    for rec in corpus_inputs():
        outs = [run_case(c) for c in expand(rec)]
        if "bps" in rec:
            rec["outs"] = outs
        else:
            rec["out"] = outs[0]
        lines.append(json.dumps(rec, separators=(",", ":"), sort_keys=True))
    GOLDEN.write_text("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} records to {GOLDEN} ({GOLDEN.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
