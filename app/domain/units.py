"""Spoken-unit render/parse helpers (money, pct, date, count).

Money is integer cents. Settlement percentages are integer basis points
(4500 = 45%). Spoken forms never come from the LLM — only from these renderers.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_DATE_RE = re.compile(
    r"^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?$",
)


def render_money(cents: int) -> str:
    """Render cents as `$2,500` or `$2,500.50`."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    dollars, rem = divmod(cents, 100)
    whole = f"{dollars:,}"
    if rem == 0:
        return f"{sign}${whole}"
    return f"{sign}${whole}.{rem:02d}"


def parse_money(text: str) -> int:
    """Parse `$2,500` / `$2,500.50` / `2500.50` into integer cents."""
    s = text.strip().replace(",", "")
    if s.startswith("$"):
        s = s[1:].strip()
    if not s:
        raise ValueError(f"empty money string: {text!r}")
    return int(Decimal(s) * 100)


def render_pct(bp: int) -> str:
    """Render basis points as `45%` or `45.5%`."""
    pct = Decimal(bp) / Decimal(100)
    if pct == pct.to_integral_value():
        return f"{int(pct)}%"
    formatted = format(pct, "f").rstrip("0").rstrip(".")
    return f"{formatted}%"


def parse_pct(text: str) -> int:
    """Parse `45%` / `45.5%` / `45.5` into integer basis points."""
    s = text.strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    if not s:
        raise ValueError(f"empty pct string: {text!r}")
    return int(Decimal(s) * 100)


def render_date(d: date, ref: date) -> str:
    """Render `January 31`, adding `, 2027` when the year differs from `ref`."""
    base = f"{d.strftime('%B')} {d.day}"
    if d.year != ref.year:
        return f"{base}, {d.year}"
    return base


def parse_date(text: str, ref: date) -> date:
    """Parse `January 31` or `January 31, 2027` (year defaults to `ref.year`)."""
    s = text.strip()
    m = _DATE_RE.match(s)
    if not m:
        raise ValueError(f"unrecognized date: {text!r}")
    month_name = m.group("month").lower()
    if month_name not in _MONTHS:
        raise ValueError(f"unknown month: {m.group('month')!r}")
    day = int(m.group("day"))
    year = int(m.group("year")) if m.group("year") else ref.year
    return date(year, _MONTHS[month_name], day)


def render_count(n: int) -> str:
    """Render a count as digits, e.g. `6`."""
    return str(n)


def parse_count(text: str) -> int:
    """Parse a digit count string."""
    return int(text.strip())


def bp_to_decimal(bp: int) -> Decimal:
    """Convert basis points to a Decimal fraction for the engine."""
    return Decimal(bp) / Decimal(10000)
