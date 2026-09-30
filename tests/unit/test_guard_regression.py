"""Regression suite for template and rendered guards (adversarial corpus)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.agent.guards import rendered_guard, template_guard
from app.domain.facts import Fact, FactSet

_CORPUS = Path(__file__).with_name("guard_adversarial.jsonl")

_ALLOWED_IDS = frozenset(
    {
        "offer_total",
        "num_payments",
        "settlement_pct",
        "first_payment_date",
        "payment_level",
        "last_payment",
    }
)

_REF = date(2026, 3, 1)


def _public_facts() -> FactSet:
    fs = FactSet()
    fs.add(
        Fact(
            id="offer_total",
            kind="money",
            value=75_000,
            visibility="PUBLIC",
            source="engine",
        )
    )
    fs.add(
        Fact(
            id="num_payments",
            kind="count",
            value=6,
            visibility="PUBLIC",
            source="engine",
        )
    )
    fs.add(
        Fact(
            id="settlement_pct",
            kind="pct",
            value=4500,
            visibility="PUBLIC",
            source="engine",
        )
    )
    fs.add(
        Fact(
            id="first_payment_date",
            kind="date",
            value=date(2026, 4, 15),
            visibility="PUBLIC",
            source="engine",
        )
    )
    fs.add(
        Fact(
            id="payment_level",
            kind="money",
            value=12_500,
            visibility="PUBLIC",
            source="engine",
        )
    )
    return fs


# Private $2,500 (cents) — must not appear in any rendering.
_PRIVATE_BLOCKLIST: set[tuple[str, int | date]] = {("money", 250_000)}
_CREDITOR_NUMBERS: set[tuple[str, int | date]] = set()


def _load_cases() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with _CORPUS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


_CASES = _load_cases()


def test_adversarial_corpus_size() -> None:
    assert len(_CASES) >= 40


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[f"{i}:{c['expect']}:{c['reason'] or 'ok'}" for i, c in enumerate(_CASES)],
)
def test_guard_adversarial(case: dict[str, str]) -> None:
    text = case["text"]
    stage = case["stage"]
    expect = case["expect"]
    reason = case["reason"]

    if stage == "template":
        result = template_guard(text, _ALLOWED_IDS, set())
    elif stage == "rendered":
        result = rendered_guard(
            text,
            _public_facts(),
            _CREDITOR_NUMBERS,
            _PRIVATE_BLOCKLIST,
            ref=_REF,
        )
    else:
        raise AssertionError(f"unknown stage: {stage!r}")

    if expect == "pass":
        assert result.ok, (
            f"expected pass, got block reason={result.reason!r} "
            f"tokens={result.offending}"
        )
    elif expect == "block":
        assert not result.ok, f"expected block ({reason}), got pass"
        assert result.reason == reason, (
            f"expected reason {reason!r}, got {result.reason!r}"
        )
    else:
        raise AssertionError(f"unknown expect: {expect!r}")
