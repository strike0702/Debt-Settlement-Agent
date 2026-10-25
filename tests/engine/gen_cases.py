"""Seeded random engine cases for the golden corpus, the oracle tests and the gap report.

A *case* is a plain JSON-safe dict (client, offer amounts, ``settlement_bp``, rules
with ``program_fee_bp``) so it can be stored verbatim in ``golden/golden.jsonl`` and
rebuilt into engine objects with ``case_to_objects``. Two sizes:

- ``random_case(rng, small=False)``: realistic cents, horizon 3–24 months, committed
  debits, tiers, token limits, all three structures, segments 1–4, mid-month and EOM
  first payment dates. Used by the golden corpus.
- ``random_case(rng, small=True)``: the same knobs scaled down (S ≤ ~400 cents,
  k_max ≤ 6, M ≤ 8) so ``tests/engine/oracle.py`` can enumerate every legal vector.
- ``adversarial_case(rng)`` (Phase 57): full-size staircase-heavy cases with a planted
  vector, shared-factor run lengths, tiers/tokens inside runs and tight window cash,
  built by ``planted_case`` (also used for structural witnesses in ``hunt.py``).
  Appended after ``random_case`` so the golden corpus stream is unchanged.

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


# --- adversarial full-size cases (Phase 57 hunt) ------------------------------

_SHARED_RUNS = ((2, 4), (4, 2), (3, 6), (6, 3), (2, 6), (6, 2), (4, 6), (6, 4), (2, 2), (3, 3),
                (4, 4), (3, 9), (4, 8))


def adv_floors(min_pay: int, tokens: int, tiers: list[list[int]], k: int) -> list[int]:
    """1-indexed floors (base minimum, tiers, +1 cent after the token limit)."""
    out = [0] * (k + 1)
    for i in range(1, k + 1):
        f = min_pay
        for frm, m in tiers:
            if i >= frm:
                f = max(f, m)
        if i > tokens:
            f = max(f, min_pay + 1)
        out[i] = f
    return out


def planted_case(
    case_id: str,
    planted: list[int],
    rules: dict[str, Any],
    *,
    t1: date,
    F: int,
    window_slack: list[int],
    tail_extra: list[int],
    as_of_days: int = 10,
    balance: int = 0,
    dip: dict[int, int] | None = None,
    lead_days: list[int] | None = None,
) -> Case:
    """A case whose window cash is ``planted``'s prefix sums plus bank fees plus slack.

    ``window_slack[j-1]`` is the slack of window j < k (cap minus the planted prefix);
    ``tail_extra`` (one per window k..M) is the cash left above S plus k bank fees, its
    last entry is the room for the fee F. Net cash for window j lands as one credit up
    to ``lead_days[j-1]`` days before ``t_j`` (or one debit on ``t_j`` when the net is
    negative), so each window's minimum is exactly its target; ``dip`` adds a
    debit/credit pair inside window j that lowers its minimum by the given amount.
    S = sum(planted); the offer is 50 % of 2S, the fee 20 % of 5F (both exact).
    """
    k = len(planted)
    bf = int(rules["bank_fee_cents"])
    M = k + len(tail_extra) - 1
    cad = monthly_payment_dates(t1, M)
    S = sum(planted)
    target: list[int] = []
    run = 0
    for j in range(1, k):
        run += planted[j - 1]
        target.append(run + bf * j + window_slack[j - 1])
    target += [S + bf * k + e for e in tail_extra]
    as_of = t1 - timedelta(days=as_of_days)
    ledger: list[list[Any]] = []
    prev_cash = balance
    for j, d in enumerate(cad):
        net = target[j] - prev_cash
        prev_cash = target[j]
        if net > 0 or j == M - 1:
            lead = lead_days[j] if lead_days and j < M - 1 else 0
            when = max(d - timedelta(days=lead), as_of + timedelta(days=1))
            ledger.append([when.isoformat(), net, "credit"])
            if net <= 0:  # the horizon needs a draft on t_M
                ledger[-1][1] = 1
                ledger.append([d.isoformat(), 1 - net, "debit"])
        elif net < 0:
            ledger.append([d.isoformat(), -net, "debit"])
        if dip and dip.get(j + 1) and j < M - 1:
            ledger.append([(d + timedelta(days=5)).isoformat(), dip[j + 1], "debit"])
            ledger.append([(d + timedelta(days=6)).isoformat(), dip[j + 1], "credit"])
    ledger.sort(key=lambda e: (e[0], e[2] != "credit"))
    credits = [e for e in ledger if e[2] == "credit"]
    first_draft = date.fromisoformat(credits[0][0])
    draft = max(1, sorted(e[1] for e in credits)[len(credits) // 2])
    return {
        "id": case_id,
        "client": {
            "draft_amount_cents": draft,
            "draft_day": first_draft.day,
            "first_draft_date": first_draft.isoformat(),
            "last_draft_date": cad[-1].isoformat(),
            "as_of_date": as_of.isoformat(),
            "current_balance_cents": balance,
            "ledger": ledger,
        },
        "creditor_balance_cents": 2 * S,
        "original_balance_cents": 5 * F if F else 2 * S,
        "settlement_bp": 5000,
        "first_payment_date": t1.isoformat(),
        "rules": {**rules, "program_fee_bp": 2000 if F else 0},
        "planted": list(planted),
    }


def adversarial_case(rng: Random, case_id: str) -> Case:
    """A full-size case aimed at the legacy generator's pinned early staircase levels.

    Staircase-heavy (80 %, ``max_segments`` 3–4), run lengths whose last two runs often
    share a factor (2 and 4, 3 and 6, ...), tiers and token limits placed inside runs, and
    a *planted* vector whose early levels sit a little above their lower bounds, so S is
    often not reachable with those levels pinned. Window cash is the planted prefix sum
    (or, in "cheap" mode, the floors-only prefix sum) plus a slack that is exactly 0–3
    cents in "tight" mode; some slacks are negative (infeasible cases, for the rescue
    comparison). First payment dates are mid-month or end of month.
    """
    roll = rng.random()
    even, balloon = (True, False) if roll < 0.1 else ((False, True) if roll < 0.2 else
                                                       (False, False))
    segs = rng.choice([3, 4]) if not (even or balloon) else rng.randint(1, 4)
    m = min(rng.choice([2, 3, 3, 4, 4]) if segs >= 3 else rng.randint(1, segs), segs)
    if m >= 2 and rng.random() < 0.7:
        counts = [rng.choice([1, 1, 2, 3, rng.randint(1, 6)]) for _ in range(m - 2)]
        counts += list(rng.choice(_SHARED_RUNS))
    else:
        counts = [rng.randint(1, 6) for _ in range(m)]
    while sum(counts) > 24:
        i = max(range(m), key=lambda j: counts[j])
        counts[i] -= 1
    k = sum(counts)
    ends = [sum(counts[: i + 1]) for i in range(m)]

    min_pay = rng.randint(10, 150) * 100 + rng.choice([0, 0, rng.randint(1, 99)])
    tokens = rng.choice([k, rng.randint(0, k), rng.choice(ends) - rng.randint(0, 1)])
    tokens = max(0, min(tokens, k))
    tiers: list[list[int]] = []
    for _ in range(rng.choice([0, 1, 1, 2])):
        frm = rng.randint(2, max(2, k))
        tiers.append([frm, min_pay + rng.choice([1, rng.randint(1, 99), rng.randint(1, 50) * 100])])
    tiers.sort()
    f = adv_floors(min_pay, tokens, tiers, k)

    levels: list[int] = []
    prev = 0
    for i in range(m):
        lo = max(f[ends[i]], prev + 1) if i else f[ends[i]]
        if i < m - 2:
            lift = rng.choice([1, 1, 2, 3, rng.randint(1, 9), rng.randint(1, 5000)])
        else:
            lift = rng.choice([0, 1, rng.randint(0, 99), rng.randint(0, 20000)])
        levels.append(lo + lift)
        prev = levels[-1]
    planted: list[int] = []
    for c, v in zip(counts, levels):
        planted.extend([v] * c)
    if even:
        q, r = divmod(sum(planted), k)
        planted = [q] * (k - r) + [q + 1] * r

    bank_fee = rng.choice([0, rng.randint(1, 15) * 100, rng.randint(1, 1500)])
    F = rng.choice([0, rng.randint(1, 400) * 100, rng.randint(1, 60000)])
    M = k + rng.choice([0, 0, 1, 2, 3])
    scale = max(1, F, min_pay // 4)
    tight = rng.random() < 0.35

    def slack() -> int:
        if tight:
            return rng.randint(0, 3)
        return rng.choice([0, rng.randint(0, 50), rng.randint(0, scale), rng.randint(0, 2 * scale)])

    window_slack = []
    run_p = run_f = 0
    cheap = rng.random() < 0.3
    for j in range(1, k):
        run_p += planted[j - 1]
        run_f += f[j]
        # Cheap mode: caps just above the floors-only prefix (often below the planted one).
        window_slack.append((run_f - run_p if cheap else 0) + slack())
    if rng.random() < 0.25 and k > 1:
        window_slack[rng.randint(0, k - 2)] -= rng.randint(1, max(1, scale // 2))
    tail_extra = sorted(slack() for _ in range(M - k + 1))
    tail_extra[-1] = max(tail_extra[-1], F + rng.choice([0, 0, rng.randint(0, 100)]))
    if rng.random() < 0.15:
        tail_extra[-1] -= rng.randint(1, max(1, scale // 4))

    year = 2026 + rng.randint(0, 1)
    month = rng.randint(1, 12)
    if rng.random() < 0.4:
        t1 = end_of_month(date(year, month, 1))
    else:
        day = rng.choice([1, 5, 10, 15, 20, 28, 29, 30])
        t1 = date(year, month, min(day, end_of_month(date(year, month, 1)).day))
    max_payments = rng.choice([k, k, min(24, M)])
    rules = {
        "max_terms": max_payments,
        "max_payments": max_payments,
        "min_payment_cents": min_pay,
        "max_token_pays": tokens,
        "min_payment_tiers": tiers,
        "even_pays": even,
        "is_ballooning_allowed": balloon,
        "max_segments": segs,
        "bank_fee_cents": bank_fee,
    }
    dip = {j: rng.randint(1, scale) for j in range(1, M) if rng.random() < 0.1}
    return planted_case(
        case_id,
        planted,
        rules,
        t1=t1,
        F=F,
        window_slack=window_slack,
        tail_extra=tail_extra,
        as_of_days=rng.randint(4, 40),
        balance=rng.choice([0, 0, rng.randint(0, min_pay)]),
        dip=dip,
        lead_days=[rng.choice([0, 0, 1, 2, 3]) for _ in range(M)],
    )


def adversarial_cases(seed: int, n: int, *, prefix: str = "a") -> list[Case]:
    """``n`` adversarial cases from one ``Random(seed)`` stream."""
    rng = Random(seed)
    return [adversarial_case(rng, f"{prefix}{seed}_{i:05d}") for i in range(n)]
