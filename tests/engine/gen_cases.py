"""Seeded random engine cases for the golden corpus, the oracle tests and the gap report.

A *case* is a plain JSON-safe dict (client, offer amounts, ``settlement_bp``, rules
with ``program_fee_bp``) so it can be stored verbatim in ``golden/golden.jsonl`` and
rebuilt into engine objects with ``case_to_objects``. Two sizes:

- ``random_case(rng, small=False)``: realistic cents, horizon 3–24 months, committed
  debits, tiers, token limits, all three structures, segments 1–4, mid-month and EOM
  first payment dates. Used by the golden corpus.
- ``random_case(rng, small=True)``: the same knobs scaled down (S ≤ ~400 cents,
  k_max ≤ 6, M ≤ 8) so ``tests/engine/oracle.py`` can enumerate every legal vector.

Only stdlib ``random`` (seeded by the caller). Not imported by ``app/``.
"""

from __future__ import annotations

from datetime import date, timedelta
from random import Random
from typing import Any

from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    add_months,
    end_of_month,
    monthly_payment_dates,
)

Case = dict[str, Any]


def case_to_objects(case: Case) -> tuple[Client, Offer, CreditorRules]:
    """Rebuild engine inputs from a stored case dict."""
    c = case["client"]
    client = Client(
        draft_amount_cents=int(c["draft_amount_cents"]),
        draft_day=int(c["draft_day"]),
        first_draft_date=date.fromisoformat(c["first_draft_date"]),
        last_draft_date=date.fromisoformat(c["last_draft_date"]),
        as_of_date=date.fromisoformat(c["as_of_date"]),
        current_balance_cents=int(c["current_balance_cents"]),
        ledger=[LedgerEntry(date.fromisoformat(d), int(a), t) for d, a, t in c["ledger"]],
    )
    fpd = case["first_payment_date"]
    offer = Offer(
        creditor=case.get("creditor", "GenCo"),
        creditor_balance_cents=int(case["creditor_balance_cents"]),
        original_balance_cents=int(case["original_balance_cents"]),
        settlement_bp=int(case["settlement_bp"]),
        first_payment_date=date.fromisoformat(fpd) if fpd else None,
    )
    r = case["rules"]
    rules = CreditorRules(
        max_terms=int(r["max_terms"]),
        max_payments=int(r["max_payments"]),
        min_payment_cents=int(r["min_payment_cents"]),
        max_token_pays=int(r["max_token_pays"]),
        min_payment_tiers=[(int(a), int(b)) for a, b in r["min_payment_tiers"]],
        even_pays=bool(r["even_pays"]),
        is_ballooning_allowed=bool(r["is_ballooning_allowed"]),
        max_segments=int(r["max_segments"]),
        bank_fee_cents=int(r["bank_fee_cents"]),
        program_fee_bp=int(r["program_fee_bp"]),
    )
    return client, offer, rules


def client_to_dict(client: Client) -> dict[str, Any]:
    """JSON-safe client (ledger as ``[iso_date, cents, type]`` triples)."""
    return {
        "draft_amount_cents": client.draft_amount_cents,
        "draft_day": client.draft_day,
        "first_draft_date": client.first_draft_date.isoformat(),
        "last_draft_date": client.last_draft_date.isoformat(),
        "as_of_date": client.as_of_date.isoformat(),
        "current_balance_cents": client.current_balance_cents,
        "ledger": [[e.date.isoformat(), e.amount_cents, e.type] for e in client.ledger],
    }


def _structure(rng: Random) -> tuple[bool, bool]:
    return rng.choice([(True, False), (False, True), (False, False)])


def random_case(rng: Random, case_id: str, *, small: bool = False) -> Case:
    """One seeded random case; ``small`` keeps it inside the oracle's enumeration budget."""
    as_of = date(2026, 1, 1) + timedelta(days=rng.randint(0, 500))
    first_draft = as_of + timedelta(days=rng.randint(1, 25))
    n_drafts = rng.randint(2, 7) if small else rng.randint(3, 24)
    draft_dates = monthly_payment_dates(first_draft, n_drafts)
    if small:
        draft = rng.randint(15, 120)
        balance = rng.choice([0, 0, rng.randint(0, 2 * draft)])
    else:
        draft = rng.randint(50, 600) * 100
        balance = rng.choice([0, rng.randint(0, 3 * draft)])
    ledger: list[list[Any]] = [[d.isoformat(), draft, "credit"] for d in draft_dates]
    # A stale entry on/before as_of must be ignored by every simulator.
    if rng.random() < 0.2:
        ledger.append([as_of.isoformat(), draft, "debit"])
    for _ in range(rng.choice([0, 0, 1, 2, 3, 4])):
        d = first_draft + timedelta(days=rng.randint(0, (draft_dates[-1] - first_draft).days + 3))
        ledger.append([d.isoformat(), max(1, int(draft * rng.uniform(0.1, 0.8))), "debit"])
    ledger.sort(key=lambda e: e[0])

    roll = rng.random()
    if roll < 0.34:
        fpd: date | None = None
    elif roll < 0.67:
        fpd = end_of_month(add_months(first_draft, rng.randint(0, 2)))
    else:
        base = add_months(first_draft, rng.randint(0, 1))
        day = rng.choice([1, 5, 10, 15, 20, 28, 29, 30])
        fpd = date(base.year, base.month, min(day, end_of_month(base).day))
        if fpd < first_draft:
            fpd = first_draft

    even, balloon = _structure(rng)
    k_cap = 6 if small else 24
    max_payments = rng.randint(1, min(k_cap, n_drafts + 1))
    max_terms = rng.choice([max_payments, max_payments, rng.randint(1, k_cap)])
    if small:
        min_pay = rng.randint(3, 50)
        bank_fee = rng.choice([0, 0, rng.randint(1, 8)])
        fee_bp = rng.choice([0, rng.randint(1, 30) * 100, rng.randint(1, 2500)])
    else:
        min_pay = rng.randint(10, 150) * 100
        bank_fee = rng.choice([0, rng.randint(1, 15) * 100, rng.randint(1, 1500)])
        fee_bp = rng.choice([0, rng.randint(5, 25) * 100, rng.randint(1, 2500)])
    tiers: list[list[int]] = []
    for _ in range(rng.choice([0, 0, 1, 2])):
        frm = rng.randint(1, max_payments + 1)
        tiers.append([frm, min_pay + rng.randint(1, max(2, min_pay))])
    tiers.sort()

    capacity = balance + draft * n_drafts
    capacity -= sum(e[1] for e in ledger if e[2] == "debit" and e[0] > as_of.isoformat())
    target = max(min_pay, int(max(capacity, draft) * rng.uniform(0.15, 1.0)))
    if small:
        target = min(target, 400)
        bp = rng.randint(20, 90) * 100 if rng.random() < 0.5 else rng.randint(2000, 9000)
    else:
        bp = rng.choice([rng.randint(10, 90) * 100, rng.randint(40, 360) * 25])
    creditor_balance = max(1, target * 10000 // bp)
    original = creditor_balance + rng.randint(0, creditor_balance // 2)
    if small:
        # Keep the program fee in the same scale as the drafts.
        original = min(original, 6 * draft)

    return {
        "id": case_id,
        "client": {
            "draft_amount_cents": draft,
            "draft_day": first_draft.day,
            "first_draft_date": first_draft.isoformat(),
            "last_draft_date": draft_dates[-1].isoformat(),
            "as_of_date": as_of.isoformat(),
            "current_balance_cents": balance,
            "ledger": ledger,
        },
        "creditor_balance_cents": creditor_balance,
        "original_balance_cents": original,
        "settlement_bp": bp,
        "first_payment_date": fpd.isoformat() if fpd else None,
        "rules": {
            "max_terms": max_terms,
            "max_payments": max_payments,
            "min_payment_cents": min_pay,
            "max_token_pays": rng.randint(0, max_payments),
            "min_payment_tiers": tiers,
            "even_pays": even,
            "is_ballooning_allowed": balloon,
            "max_segments": rng.randint(1, 4),
            "bank_fee_cents": bank_fee,
            "program_fee_bp": fee_bp,
        },
    }


def random_cases(seed: int, n: int, *, small: bool = False, prefix: str = "r") -> list[Case]:
    """``n`` cases from one ``Random(seed)`` stream (ids ``<prefix><seed>_<i>``)."""
    rng = Random(seed)
    return [random_case(rng, f"{prefix}{seed}_{i:04d}", small=small) for i in range(n)]
