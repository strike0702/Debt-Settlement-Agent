"""Shared integer-money helpers: half-up rounding, ceil division, basis points.

Every rounding in the engine goes through ``round_half_up`` (Decimal, never
builtin ``round``). ``fraction_to_bp`` / ``bp_fraction`` are the one exact bridge
between legacy fraction inputs (``0.18``) and integer basis points (1800).
Imported by every other ``feasibility`` module; no engine logic lives here.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

BP_PER_UNIT = 10000


def round_half_up(pct: float | Decimal | str, cents: int) -> int:
    """Round ``pct * cents`` half-away-from-zero to the nearest integer.

    Uses Decimal so ``0.5`` stays exact. Builtin ``round()`` is banker's
    rounding and must never be used for money.
    """
    return int(
        (Decimal(str(pct)) * Decimal(cents)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def ceil_div(a: int, b: int) -> int:
    """Ceiling of a/b for positive a and positive b."""
    return (a + b - 1) // b


def bp_fraction(bp: int) -> Decimal:
    """Integer basis points -> exact Decimal fraction (4500 -> Decimal("0.45"))."""
    return Decimal(int(bp)) / Decimal(BP_PER_UNIT)


def fraction_to_bp(value: float | Decimal | str | int) -> int:
    """Exact fraction -> integer bp; raises ``ValueError`` if not a whole basis point.

    Floats go through ``str()`` (shortest repr), so ``0.18`` -> 1800 exactly while
    ``0.18000000000000002`` or ``0.17335`` are rejected rather than rounded.
    """
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a fraction")
    scaled = Decimal(str(value)) * BP_PER_UNIT
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise ValueError(f"{value!r} is not a whole basis point")
    return int(scaled)
