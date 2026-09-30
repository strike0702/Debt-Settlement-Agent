"""One failing case per binding validator rule; engine fixtures must pass."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.adapter.validator import (
    BINDING_RULES,
    RULE_BALANCE_MATCH,
    RULE_CADENCE,
    RULE_EVEN_VECTOR,
    RULE_EXACT_SUM,
    RULE_FEES_HORIZON,
    RULE_FLOORS,
    RULE_LEDGER,
    RULE_NON_DECREASING,
    RULE_SEGMENTS,
    RULE_TOKEN_COUNT,
    validate,
)
from feasibility.engine import ScheduleRow, evaluate_offer
from feasibility.models import CreditorRules, load_case, offer_total_cents, program_fee_cents

ENGINE = Path(__file__).resolve().parents[2] / "fixtures" / "engine"


def _good_even() -> tuple[list[ScheduleRow], object, int, int, CreditorRules, date]:
    client, offer, rules = load_case(ENGINE / "even_ok")
    result = evaluate_offer(client, offer, rules)
    assert result.feasible and result.schedule
    fpd = offer.first_payment_date or result.schedule[0].date
    return (
        list(result.schedule),
        client,
        offer_total_cents(offer),
        program_fee_cents(offer, rules),
        rules,
        fpd,
    )


def _rules_of(**kwargs: object) -> CreditorRules:
    _, _, _, _, base, _ = _good_even()
    data = {
        "max_terms": base.max_terms,
        "max_payments": base.max_payments,
        "min_payment_cents": base.min_payment_cents,
        "max_token_pays": base.max_token_pays,
        "min_payment_tiers": list(base.min_payment_tiers),
        "even_pays": base.even_pays,
        "is_ballooning_allowed": base.is_ballooning_allowed,
        "max_segments": base.max_segments,
        "bank_fee_cents": base.bank_fee_cents,
        "program_fee_pct": base.program_fee_pct,
    }
    data.update(kwargs)
    return CreditorRules(**data)  # type: ignore[arg-type]


def test_binding_rules_count() -> None:
    assert len(BINDING_RULES) == 10


def test_fail_cadence() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    rows = deepcopy(rows)
    rows[0] = ScheduleRow(
        date=rows[0].date + timedelta(days=3),
        creditor_payment_cents=rows[0].creditor_payment_cents,
        program_fee_cents=rows[0].program_fee_cents,
        bank_fee_cents=rows[0].bank_fee_cents,
        balance_cents=rows[0].balance_cents,
    )
    v = validate(rows, client, total, fee, rules, fpd)
    assert any(x.rule == RULE_CADENCE for x in v)


def test_fail_exact_sum() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    v = validate(rows, client, total + 1, fee, rules, fpd)
    assert any(x.rule == RULE_EXACT_SUM for x in v)


def test_fail_non_decreasing() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    rows = deepcopy(rows)
    # Swap to create a decrease while keeping sum (adjust two payments).
    p0 = rows[0].creditor_payment_cents
    p1 = rows[1].creditor_payment_cents
    rows[0] = ScheduleRow(
        rows[0].date,
        p0 + 500,
        rows[0].program_fee_cents,
        rows[0].bank_fee_cents,
        rows[0].balance_cents,
    )
    rows[1] = ScheduleRow(
        rows[1].date,
        p1 - 500,
        rows[1].program_fee_cents,
        rows[1].bank_fee_cents,
        rows[1].balance_cents,
    )
    # Use flexible rules so even_vector does not also fire first-path confusion.
    rules = _rules_of(even_pays=False, is_ballooning_allowed=True, max_segments=6)
    v = validate(rows, client, total, fee, rules, fpd)
    assert any(x.rule == RULE_NON_DECREASING for x in v)


def test_fail_floors() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    # Raise min payment above every installment.
    rules = _rules_of(min_payment_cents=50000, max_token_pays=6, even_pays=False)
    v = validate(rows, client, total, fee, rules, fpd)
    assert any(x.rule == RULE_FLOORS for x in v)


def test_fail_token_count() -> None:
    client, offer, base = load_case(ENGINE / "balloon_ok")
    result = evaluate_offer(client, offer, base)
    assert result.feasible and result.schedule
    fpd = offer.first_payment_date or result.schedule[0].date
    total = offer_total_cents(offer)
    fee = program_fee_cents(offer, base)
    # balloon_ok has token pays at min; clamp max_token_pays to 0.
    rules = CreditorRules(
        max_terms=base.max_terms,
        max_payments=base.max_payments,
        min_payment_cents=base.min_payment_cents,
        max_token_pays=0,
        min_payment_tiers=list(base.min_payment_tiers),
        even_pays=False,
        is_ballooning_allowed=True,
        max_segments=base.max_segments,
        bank_fee_cents=base.bank_fee_cents,
        program_fee_pct=base.program_fee_pct,
    )
    v = validate(list(result.schedule), client, total, fee, rules, fpd)
    assert any(x.rule == RULE_TOKEN_COUNT for x in v)


def test_fail_even_vector() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    rows = deepcopy(rows)
    # Distort away from even while keeping sum and non-decreasing.
    rows[0] = ScheduleRow(
        rows[0].date,
        rows[0].creditor_payment_cents - 100,
        rows[0].program_fee_cents,
        rows[0].bank_fee_cents,
        rows[0].balance_cents,
    )
    rows[-1] = ScheduleRow(
        rows[-1].date,
        rows[-1].creditor_payment_cents + 100,
        rows[-1].program_fee_cents,
        rows[-1].bank_fee_cents,
        rows[-1].balance_cents,
    )
    v = validate(rows, client, total, fee, rules, fpd)
    assert any(x.rule == RULE_EVEN_VECTOR for x in v)


def test_fail_segments() -> None:
    rows, client, total, fee, _, fpd = _good_even()
    rows = deepcopy(rows)
    # Three distinct levels, max_segments=2, no balloon exemption.
    pays = [2000, 3000, 4000, 5000, 6000, total - (2000 + 3000 + 4000 + 5000 + 6000)]
    assert pays[-1] >= pays[-2]
    new_rows = []
    for r, p in zip(rows, pays):
        new_rows.append(
            ScheduleRow(r.date, p, r.program_fee_cents, r.bank_fee_cents, r.balance_cents)
        )
    rules = _rules_of(
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=2,
        min_payment_cents=1000,
        max_token_pays=6,
    )
    v = validate(new_rows, client, total, fee, rules, fpd)
    assert any(x.rule == RULE_SEGMENTS for x in v)


def test_fail_fees_horizon() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    v = validate(rows, client, total, fee + 1, rules, fpd)
    assert any(x.rule == RULE_FEES_HORIZON for x in v)


def test_fail_ledger_nonnegative() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    rows = deepcopy(rows)
    # Inflate first debit so replay goes negative (also breaks balance_match).
    rows[0] = ScheduleRow(
        rows[0].date,
        rows[0].creditor_payment_cents + 50_000,
        rows[0].program_fee_cents,
        rows[0].bank_fee_cents,
        rows[0].balance_cents,
    )
    # Adjust last payment down so sum still matches — may go negative on first day.
    # Keep sum wrong-free by cutting last payment.
    last = rows[-1]
    rows[-1] = ScheduleRow(
        last.date,
        last.creditor_payment_cents - 50_000,
        last.program_fee_cents,
        last.bank_fee_cents,
        last.balance_cents,
    )
    rules = _rules_of(
        even_pays=False,
        is_ballooning_allowed=True,
        max_segments=6,
        min_payment_cents=1,
    )
    v = validate(rows, client, total, fee, rules, fpd)
    assert any(x.rule == RULE_LEDGER for x in v)


def test_fail_balance_match() -> None:
    rows, client, total, fee, rules, fpd = _good_even()
    rows = deepcopy(rows)
    rows[0] = ScheduleRow(
        rows[0].date,
        rows[0].creditor_payment_cents,
        rows[0].program_fee_cents,
        rows[0].bank_fee_cents,
        rows[0].balance_cents + 1,
    )
    v = validate(rows, client, total, fee, rules, fpd)
    assert any(x.rule == RULE_BALANCE_MATCH for x in v)


@pytest.mark.parametrize("rule", BINDING_RULES)
def test_each_binding_rule_has_dedicated_failure(rule: str) -> None:
    """Meta-check: the named fail_* tests above cover every rule id."""
    dedicated = {
        RULE_CADENCE: test_fail_cadence,
        RULE_EXACT_SUM: test_fail_exact_sum,
        RULE_NON_DECREASING: test_fail_non_decreasing,
        RULE_FLOORS: test_fail_floors,
        RULE_TOKEN_COUNT: test_fail_token_count,
        RULE_EVEN_VECTOR: test_fail_even_vector,
        RULE_SEGMENTS: test_fail_segments,
        RULE_FEES_HORIZON: test_fail_fees_horizon,
        RULE_LEDGER: test_fail_ledger_nonnegative,
        RULE_BALANCE_MATCH: test_fail_balance_match,
    }
    assert rule in dedicated
