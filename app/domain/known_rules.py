"""Creditor rules the operator already knows before the call (Phase 69).

Settlement firms usually know a creditor's rules before dialling. The operator
enters them in the "Known creditor rules" panel (or the firm saves them per
creditor in ``firm.json`` under ``known_creditor_rules``), and ``CallSession``
seeds them into the belief as KNOWN with source ``operator``, so the agent
neither asks for them nor assumes a default.

This module only validates and converts: wire JSON (``start.known_rules``, the
firm file) ↔ belief values (cents ints, ``date``, tiers as ``(from, min_cents)``
tuples). It does not touch the belief (``BeliefState.seed_operator``) or the
socket (``app.voice.ws``). The rules are creditor terms, not client finances.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.domain.fields import PAYMENT_STRUCTURES

# Belief field names, in the panel's order.
KNOWN_RULE_FIELDS: tuple[str, ...] = (
    "max_payments",
    "min_payment_cents",
    "payment_structure",
    "min_payment_tiers",
    "max_segments",
    "max_token_pays",
    "first_payment_date",
)
MAX_PAYMENTS = 60
MAX_SEGMENTS = 10
MAX_MIN_PAYMENT_CENTS = 10_000_000  # $100,000: sane upper bound for a payment floor


def _whole(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _count(raw: dict[str, Any], key: str, lo: int, hi: int, what: str) -> int | None:
    v = raw.get(key)
    if v is None:
        return None
    if not _whole(v) or not lo <= v <= hi:
        raise ValueError(f"known_rules.{key}: {what} must be a whole number from {lo} to {hi}.")
    return int(v)


def _tiers(raw: Any) -> list[tuple[int, int]]:
    if not isinstance(raw, list):
        raise ValueError("known_rules.min_payment_tiers: payment tiers must be a list of rows.")
    out: list[tuple[int, int]] = []
    for i, row in enumerate(raw, start=1):
        frm = row.get("from_payment") if isinstance(row, dict) else None
        cents = row.get("min_cents") if isinstance(row, dict) else None
        if not _whole(frm) or not 1 <= frm <= MAX_PAYMENTS:
            raise ValueError(
                f"known_rules.min_payment_tiers: tier {i} needs a payment number "
                f"from 1 to {MAX_PAYMENTS}."
            )
        if not _whole(cents) or not 0 < cents <= MAX_MIN_PAYMENT_CENTS:
            raise ValueError(f"known_rules.min_payment_tiers: tier {i} needs a dollar amount.")
        out.append((int(frm), int(cents)))
    return out


def parse_known_rules(
    raw: Any,
    *,
    as_of: date | None = None,
    allow_date: bool = True,
) -> dict[str, Any]:
    """Validate known rules JSON into belief values ``{field: value}``; absent/null fields left out.

    Tiers must make sense together with the other rules: each tier starts at a
    later payment than the one before and asks for more, at or above the minimum
    payment (as Phase 68's ``tier_conflict``) and no later than the maximum number
    of payments. Raises ``ValueError`` with a plain
    message that names ``known_rules.<field>``. ``allow_date=False`` (firm
    defaults) refuses a first payment date, which is set per call.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("known_rules must be an object.")
    unknown = sorted(set(raw) - set(KNOWN_RULE_FIELDS))
    if unknown:
        raise ValueError(f"known_rules: unknown rule {unknown[0]!r}.")
    out: dict[str, Any] = {}
    n = _count(raw, "max_payments", 1, MAX_PAYMENTS, "Max payments")
    if n is not None:
        out["max_payments"] = n
    floor = raw.get("min_payment_cents")
    if floor is not None:
        if not _whole(floor) or not 0 < floor <= MAX_MIN_PAYMENT_CENTS:
            raise ValueError(
                "known_rules.min_payment_cents: minimum payment must be whole cents above 0, "
                "e.g. 10000 for $100.00."
            )
        out["min_payment_cents"] = int(floor)
    structure = raw.get("payment_structure")
    if structure is not None:
        if structure not in PAYMENT_STRUCTURES:
            raise ValueError(
                "known_rules.payment_structure: "
                "payment structure must be even, balloon or flexible."
            )
        out["payment_structure"] = str(structure)
    if raw.get("min_payment_tiers") is not None:
        tiers = _tiers(raw["min_payment_tiers"])
        for (f0, c0), (f1, c1) in zip(tiers, tiers[1:], strict=False):
            if f1 <= f0 or c1 <= c0:
                raise ValueError(
                    "known_rules.min_payment_tiers: each tier must start at a later payment "
                    "and ask for more than the one before."
                )
        if tiers and n is not None and tiers[-1][0] > n:
            raise ValueError(
                f"known_rules.min_payment_tiers: a tier starts at payment {tiers[-1][0]}, "
                f"after the last allowed payment ({n})."
            )
        if tiers and floor is not None and tiers[0][1] < floor:
            raise ValueError(
                "known_rules.min_payment_tiers: no tier can ask for less than the "
                "minimum payment."
            )
        out["min_payment_tiers"] = tiers
    segments = _count(raw, "max_segments", 1, MAX_SEGMENTS, "Max payment levels")
    if segments is not None:
        out["max_segments"] = segments
    tokens = _count(raw, "max_token_pays", 0, MAX_PAYMENTS, "Token payments")
    if tokens is not None:
        if n is not None and tokens > n:
            raise ValueError(
                "known_rules.max_token_pays: token payments cannot be more than max payments."
            )
        out["max_token_pays"] = tokens
    fpd = raw.get("first_payment_date")
    if fpd is not None:
        if not allow_date:
            raise ValueError(
                "known_rules.first_payment_date: the first payment date is set per call, "
                "not saved for a creditor."
            )
        try:
            day = date.fromisoformat(fpd) if isinstance(fpd, str) else None
        except ValueError:
            day = None
        if day is None:
            raise ValueError(
                "known_rules.first_payment_date: write the date as YYYY-MM-DD, e.g. 2026-12-01."
            )
        if as_of is not None and day < as_of:
            raise ValueError(
                f"known_rules.first_payment_date: the first payment cannot be before "
                f"today's date for this call ({as_of.isoformat()})."
            )
        out["first_payment_date"] = day
    return out


def known_rules_to_wire(rules: dict[str, Any]) -> dict[str, Any]:
    """Belief values → wire JSON (tiers as ``{from_payment, min_cents}``, ISO date)."""
    out: dict[str, Any] = {}
    for key in KNOWN_RULE_FIELDS:
        if key not in rules:
            continue
        v = rules[key]
        if key == "min_payment_tiers":
            out[key] = [{"from_payment": int(a), "min_cents": int(b)} for a, b in v]
        elif isinstance(v, date):
            out[key] = v.isoformat()
        else:
            out[key] = v
    return out


def rules_from_sim(raw: dict[str, Any], first_payment_date: date | None) -> dict[str, Any]:
    """A scenario's true creditor rules (``sim.json`` ``rules``) as wire JSON.

    For the panel's "Fill from scenario" (a demo convenience). ``first_payment_date``
    is the caller's resolved date (``sim.json`` dates are relative), or ``None`` for
    ``"default"``: the agent then keeps its own default.
    """
    rules = {k: raw[k] for k in KNOWN_RULE_FIELDS if raw.get(k) is not None}
    rules["min_payment_tiers"] = [(int(a), int(b)) for a, b in raw.get("min_payment_tiers", [])]
    rules.pop("first_payment_date", None)
    if first_payment_date is not None:
        rules["first_payment_date"] = first_payment_date
    return known_rules_to_wire(rules)
