"""Unit tests for number token extraction and words-to-digits."""

from __future__ import annotations

from datetime import date

import pytest

from app.agent.numbers import extract_tokens, normalize_token, words_to_number


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("two hundred fifty", 250),
        ("twenty-five hundred", 2500),
        ("twenty five", 25),
        ("one thousand", 1000),
        ("three", 3),
        ("fifty", 50),
        ("first", 1),
        ("dozen", 12),
        ("couple", 2),
        ("not a number", None),
        ("", None),
    ],
)
def test_words_to_number(phrase: str, expected: int | None) -> None:
    assert words_to_number(phrase) == expected


def test_extract_money_and_pct() -> None:
    toks = extract_tokens("Pay $2,500.00 which is 45.5%.", ref=date(2026, 1, 1))
    assert [(t.kind, t.value) for t in toks] == [
        ("money", 250_000),
        ("pct", 4550),
    ]


def test_extract_abbrev_hidden() -> None:
    toks = extract_tokens("about 2.5k please", ref=date(2026, 1, 1))
    assert len(toks) == 1
    assert toks[0].kind == "money"
    assert toks[0].value == 250_000


def test_extract_abbrev_uses_decimal_not_float() -> None:
    """F12: k/m abbrevs must not go through binary float."""
    from app.agent.numbers import _parse_abbrev

    # 0.1 * 1000 * 100 is exact in Decimal; float path is latent risk.
    assert _parse_abbrev("0.1k") == ("money", 10_000)
    assert _parse_abbrev("1.25k") == ("money", 125_000)
    assert _parse_abbrev("2.5m") == ("money", 250_000_000)


def test_extract_dates_named_iso_slash() -> None:
    ref = date(2026, 3, 1)
    named = extract_tokens("due April 15", ref=ref)
    assert named[0].kind == "date"
    assert named[0].value == date(2026, 4, 15)

    iso = extract_tokens("due 2026-07-04", ref=ref)
    assert iso[0].value == date(2026, 7, 4)

    slash = extract_tokens("due 7/4/2026", ref=ref)
    assert slash[0].value == date(2026, 7, 4)


def test_extract_ordinal_and_bare() -> None:
    toks = extract_tokens("the 15th and then 6 more", ref=date(2026, 1, 1))
    kinds = [(t.kind, t.value) for t in toks]
    assert ("ordinal", 15) in kinds
    assert ("count", 6) in kinds


def test_extract_number_words_and_dollars() -> None:
    toks = extract_tokens("twenty-five hundred dollars", ref=date(2026, 1, 1))
    assert len(toks) == 1
    assert toks[0].kind == "money"
    assert toks[0].value == 250_000


def test_normalize_token_pair() -> None:
    toks = extract_tokens("$125", ref=date(2026, 1, 1))
    assert normalize_token(toks[0]) == ("money", 12_500)


def test_span_masking_no_overlap() -> None:
    """Money match masks digits so bare-number pass does not re-emit them."""
    toks = extract_tokens("total $750 only", ref=date(2026, 1, 1))
    assert len(toks) == 1
    assert toks[0].kind == "money"
    assert toks[0].value == 75_000


def test_invalid_named_date_skipped() -> None:
    toks = extract_tokens("due February 30", ref=date(2026, 1, 1))
    assert not any(t.kind == "date" for t in toks)


def test_invalid_iso_date_skipped() -> None:
    toks = extract_tokens("due 2026-13-01", ref=date(2026, 1, 1))
    assert not any(t.kind == "date" for t in toks)
