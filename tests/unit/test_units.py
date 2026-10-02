"""Unit tests for spoken-unit render and parse (money / pct / date / count)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.units import (
    bp_to_decimal,
    parse_count,
    parse_date,
    parse_money,
    parse_pct,
    render_count,
    render_date,
    render_money,
    render_pct,
)


@pytest.mark.parametrize(
    ("cents", "spoken"),
    [
        (250000, "$2,500"),
        (250050, "$2,500.50"),
        (0, "$0"),
        (99, "$0.99"),
        (100, "$1"),
        (1_000_000_00, "$1,000,000"),
    ],
)
def test_render_parse_money_roundtrip(cents: int, spoken: str) -> None:
    assert render_money(cents) == spoken
    assert parse_money(spoken) == cents


@pytest.mark.parametrize(
    ("bp", "spoken"),
    [
        (4500, "45%"),
        (4550, "45.5%"),
        (4525, "45.25%"),
        (10000, "100%"),
        (100, "1%"),
        (1, "0.01%"),
    ],
)
def test_render_parse_pct_roundtrip(bp: int, spoken: str) -> None:
    assert render_pct(bp) == spoken
    assert parse_pct(spoken) == bp


def test_bp_to_decimal() -> None:
    assert bp_to_decimal(4500) == Decimal("0.45")


def test_parse_money_rejects_extra_decimals() -> None:
    """F11: do not truncate 10.999 → 1099."""
    with pytest.raises(ValueError, match="more than two decimals"):
        parse_money("10.999")


def test_parse_pct_half_up() -> None:
    """F11: align with ask_pct_to_bp HALF_UP (45.125 → 4513)."""
    assert parse_pct("45.125%") == 4513
    assert parse_pct("45.125") == 4513


@pytest.mark.parametrize(
    ("d", "ref", "spoken"),
    [
        (date(2026, 1, 31), date(2026, 3, 1), "January 31"),
        (date(2027, 1, 31), date(2026, 3, 1), "January 31, 2027"),
        (date(2026, 12, 5), date(2026, 1, 1), "December 5"),
        (date(2025, 12, 5), date(2026, 1, 1), "December 5, 2025"),
    ],
)
def test_render_parse_date_roundtrip(d: date, ref: date, spoken: str) -> None:
    assert render_date(d, ref) == spoken
    assert parse_date(spoken, ref) == d


@pytest.mark.parametrize(("n", "spoken"), [(6, "6"), (0, "0"), (12, "12")])
def test_render_parse_count_roundtrip(n: int, spoken: str) -> None:
    assert render_count(n) == spoken
    assert parse_count(spoken) == n
