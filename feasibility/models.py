"""Engine input models, JSON loaders, cadence/date helpers, offer total and program fee.

Money is integer cents, settlement and program fee are integer basis points
(``Offer.settlement_bp``, ``CreditorRules.program_fee_bp``), dates are
``datetime.date``. The JSON loaders still accept the take-home float keys
(``settlement_pct``, ``program_fee_pct``) and convert them exactly once with
``fraction_to_bp``. This module does no simulation or search: see ``problem.py``
(precomputed per-client state) and ``engine.py`` (``evaluate`` / ``evaluate_offer``).
Imported by ``app/`` (adapter, belief, scenario), ``sim/`` and every engine module.
"""

from __future__ import annotations

import json
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from feasibility.util import bp_fraction, fraction_to_bp, round_half_up

EntryType = Literal["credit", "debit"]


@dataclass(frozen=True)
class LedgerEntry:
    """One committed SDA ledger line (draft credit or previously committed debit)."""

    date: date
    amount_cents: int
    type: EntryType


@dataclass
class Client:
    """Client SDA: balance as of ``as_of_date`` plus the future ledger to simulate."""

    draft_amount_cents: int
    draft_day: int
    first_draft_date: date
    last_draft_date: date
    as_of_date: date
    current_balance_cents: int
    ledger: list[LedgerEntry] = field(default_factory=list)


@dataclass
class Offer:
    """One creditor offer; ``settlement_bp`` is integer basis points (4500 = 45%)."""

    creditor: str
    # Creditor's outstanding balance (not the client's SDA balance).
    # Spec name: creditor_balance_cents — distinct from Client.current_balance_cents.
    creditor_balance_cents: int
    original_balance_cents: int
    settlement_bp: int
    # Optional. When omitted, default to the end of the month of first_draft_date
    # (see default_first_payment_date()).
    first_payment_date: date | None = None


@dataclass(init=False)
class CreditorRules:
    """Creditor rules plus firm fees; ``program_fee_bp`` is integer basis points.

    Two independent creditor flags (both default False in JSON):
      even_pays             -> every creditor payment must be equal (ballooning is irrelevant).
      is_ballooning_allowed -> the final payment may absorb the remainder (a "balloon").
    When NOT ballooning (and not even), the payment structure is bounded to at most
    ``max_segments`` distinct payment levels.

    The constructor also takes the legacy keyword ``program_fee_pct`` (a fraction,
    e.g. 0.18) and converts it exactly to bp, rejecting values that are not whole
    bp. It is write-only: the stored field is ``program_fee_bp``. The keyword exists
    so ``sim.scenarios.to_creditor_rules`` keeps working until it passes bp itself.
    """

    max_terms: int
    max_payments: int
    min_payment_cents: int
    max_token_pays: int
    min_payment_tiers: list[tuple[int, int]]  # [(from_payment_1based, min_cents), ...]
    even_pays: bool
    is_ballooning_allowed: bool
    max_segments: int
    bank_fee_cents: int
    program_fee_bp: int

    def __init__(
        self,
        max_terms: int,
        max_payments: int,
        min_payment_cents: int,
        max_token_pays: int,
        min_payment_tiers: list[tuple[int, int]],
        even_pays: bool,
        is_ballooning_allowed: bool,
        max_segments: int,
        bank_fee_cents: int,
        program_fee_bp: int | None = None,
        *,
        program_fee_pct: Any = None,
    ) -> None:
        if (program_fee_bp is None) == (program_fee_pct is None):
            raise TypeError("CreditorRules needs exactly one of program_fee_bp, program_fee_pct")
        self.max_terms = max_terms
        self.max_payments = max_payments
        self.min_payment_cents = min_payment_cents
        self.max_token_pays = max_token_pays
        self.min_payment_tiers = min_payment_tiers
        self.even_pays = even_pays
        self.is_ballooning_allowed = is_ballooning_allowed
        self.max_segments = max_segments
        self.bank_fee_cents = bank_fee_cents
        self.program_fee_bp = (
            int(program_fee_bp) if program_fee_bp is not None else fraction_to_bp(program_fee_pct)
        )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def end_of_month(d: date) -> date:
    """Last day of ``d``'s month."""
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def is_end_of_month(d: date) -> bool:
    """True when ``d`` is the last day of its month."""
    return d.day == monthrange(d.year, d.month)[1]


def add_months(d: date, n: int) -> date:
    """Shift a date by ``n`` whole months, clamping the day to month length."""
    total = (d.year * 12 + (d.month - 1)) + n
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def default_first_payment_date(client: Client) -> date:
    """Default creditor first-payment date: end of the first draft's month (EOM)."""
    return end_of_month(client.first_draft_date)


def monthly_payment_dates(start: date, count: int) -> list[date]:
    """Generate ``count`` monthly dates from ``start``.

    If ``start`` is the last day of its month, every generated date is the last
    day of its month (true EOM cadence). Otherwise the day-of-month is preserved
    (clamped to month length).
    """
    if count <= 0:
        return []
    eom = is_end_of_month(start)
    out: list[date] = []
    for i in range(count):
        d = add_months(start, i)
        out.append(end_of_month(d) if eom else d)
    return out


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _d(s: str) -> date:
    return date.fromisoformat(s)


def _bp_key(raw: dict[str, Any], stem: str) -> int:
    """Read ``<stem>_bp`` (int) or the take-home ``<stem>_pct`` fraction, exactly."""
    if f"{stem}_bp" in raw:
        return int(raw[f"{stem}_bp"])
    return fraction_to_bp(raw[f"{stem}_pct"])


def load_client(path: str | Path) -> Client:
    """Load ``client.json``."""
    raw = json.loads(Path(path).read_text())
    return Client(
        draft_amount_cents=int(raw["draft_amount_cents"]),
        draft_day=int(raw["draft_day"]),
        first_draft_date=_d(raw["first_draft_date"]),
        last_draft_date=_d(raw["last_draft_date"]),
        as_of_date=_d(raw["as_of_date"]),
        current_balance_cents=int(raw["current_balance_cents"]),
        ledger=[
            LedgerEntry(_d(e["date"]), int(e["amount_cents"]), e["type"])
            for e in raw.get("ledger", [])
        ],
    )


def load_offer(path: str | Path) -> Offer:
    """Load ``offer.json`` (``settlement_bp`` or legacy ``settlement_pct``)."""
    raw = json.loads(Path(path).read_text())
    fpd = raw.get("first_payment_date")
    # Spec uses creditor_balance_cents; provided cases still use the old
    # current_balance_cents key. Accept either; prefer the new name.
    if "creditor_balance_cents" in raw:
        balance = int(raw["creditor_balance_cents"])
    else:
        balance = int(raw["current_balance_cents"])
    return Offer(
        creditor=raw["creditor"],
        creditor_balance_cents=balance,
        original_balance_cents=int(raw["original_balance_cents"]),
        settlement_bp=_bp_key(raw, "settlement"),
        first_payment_date=_d(fpd) if fpd else None,
    )


def load_creditor_rules(path: str | Path) -> CreditorRules:
    """Load ``creditor_rules.json`` (``program_fee_bp`` or legacy ``program_fee_pct``)."""
    raw = json.loads(Path(path).read_text())
    return CreditorRules(
        max_terms=int(raw["max_terms"]),
        max_payments=int(raw["max_payments"]),
        min_payment_cents=int(raw["min_payment_cents"]),
        max_token_pays=int(raw["max_token_pays"]),
        min_payment_tiers=[(int(a), int(b)) for a, b in raw.get("min_payment_tiers", [])],
        even_pays=bool(raw.get("even_pays", False)),
        is_ballooning_allowed=bool(raw.get("is_ballooning_allowed", False)),
        max_segments=int(raw.get("max_segments", 4)),
        bank_fee_cents=int(raw["bank_fee_cents"]),
        program_fee_bp=_bp_key(raw, "program_fee"),
    )


def load_case(case_dir: str | Path) -> tuple[Client, Offer, CreditorRules]:
    """Load the three JSON files of one engine case folder."""
    p = Path(case_dir)
    return (
        load_client(p / "client.json"),
        load_offer(p / "offer.json"),
        load_creditor_rules(p / "creditor_rules.json"),
    )


def offer_total_cents(offer: Offer) -> int:
    """S = round_half_up(settlement_bp / 10000 × creditor balance)."""
    return round_half_up(bp_fraction(offer.settlement_bp), offer.creditor_balance_cents)


def program_fee_cents(offer: Offer, rules: CreditorRules) -> int:
    """F = round_half_up(program_fee_bp / 10000 × original balance)."""
    return round_half_up(bp_fraction(rules.program_fee_bp), offer.original_balance_cents)
