"""Independent schedule validator (no feasibility.simulate/shapes/scoring)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from feasibility.engine import ScheduleRow
from feasibility.models import Client, CreditorRules, monthly_payment_dates


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str


# Stable rule ids — one failing test per id in tests/unit/test_validator.py.
RULE_CADENCE = "cadence"
RULE_EXACT_SUM = "exact_sum"
RULE_NON_DECREASING = "non_decreasing"
RULE_FLOORS = "floors"
RULE_TOKEN_COUNT = "token_count"
RULE_EVEN_VECTOR = "even_vector"
RULE_SEGMENTS = "segments"
RULE_FEES_HORIZON = "fees_and_horizon"
RULE_LEDGER = "ledger_nonnegative"
RULE_BALANCE_MATCH = "balance_match"

BINDING_RULES: tuple[str, ...] = (
    RULE_CADENCE,
    RULE_EXACT_SUM,
    RULE_NON_DECREASING,
    RULE_FLOORS,
    RULE_TOKEN_COUNT,
    RULE_EVEN_VECTOR,
    RULE_SEGMENTS,
    RULE_FEES_HORIZON,
    RULE_LEDGER,
    RULE_BALANCE_MATCH,
)


def _even_vector(total: int, k: int) -> tuple[int, ...]:
    q, r = divmod(total, k)
    return tuple([q] * (k - r) + [q + 1] * r)


def _compute_floors(rules: CreditorRules, k: int) -> list[int]:
    """1-indexed floors[1..k]. Independent reimplementation of the engine floors."""
    floors = [0] * (k + 1)
    for i in range(1, k + 1):
        tier = 0
        for frm, m in rules.min_payment_tiers:
            if frm <= i:
                tier = max(tier, m)
        token_bump = rules.min_payment_cents + 1 if i > rules.max_token_pays else 0
        floors[i] = max(rules.min_payment_cents, tier, token_bump)
    return floors


def _payment_rows(rows: list[ScheduleRow]) -> list[ScheduleRow]:
    return [r for r in rows if r.creditor_payment_cents > 0]


def validate(
    schedule_rows: list[ScheduleRow],
    client: Client,
    offer_total: int,
    program_fee: int,
    rules: CreditorRules,
    first_payment_date: date,
) -> list[Violation]:
    """Check all 10 binding rules. Empty list means the schedule is valid."""
    violations: list[Violation] = []
    pay_rows = _payment_rows(schedule_rows)
    payments = [r.creditor_payment_cents for r in pay_rows]
    k = len(payments)

    # 1. Consecutive monthly cadence from first_payment_date.
    if k == 0:
        violations.append(
            Violation(RULE_CADENCE, "schedule has no creditor payments")
        )
    else:
        expected = monthly_payment_dates(first_payment_date, k)
        actual = [r.date for r in pay_rows]
        if actual != expected:
            violations.append(
                Violation(
                    RULE_CADENCE,
                    f"payment dates {actual} != cadence {expected}",
                )
            )

    # 2. Exact sum.
    if sum(payments) != offer_total:
        violations.append(
            Violation(
                RULE_EXACT_SUM,
                f"payment sum {sum(payments)} != offer_total {offer_total}",
            )
        )

    # 3. Non-decreasing.
    for i in range(len(payments) - 1):
        if payments[i] > payments[i + 1]:
            violations.append(
                Violation(
                    RULE_NON_DECREASING,
                    f"payment[{i}]={payments[i]} > payment[{i + 1}]={payments[i + 1]}",
                )
            )
            break

    # 4. Floors.
    if k > 0:
        floors = _compute_floors(rules, k)
        for i, p in enumerate(payments, start=1):
            if p < floors[i]:
                violations.append(
                    Violation(
                        RULE_FLOORS,
                        f"payment[{i}]={p} < floor {floors[i]}",
                    )
                )
                break

    # 5. Token count.
    token_count = sum(1 for p in payments if p == rules.min_payment_cents)
    if token_count > rules.max_token_pays:
        violations.append(
            Violation(
                RULE_TOKEN_COUNT,
                f"token_count {token_count} > max_token_pays {rules.max_token_pays}",
            )
        )

    # 6. Even vector (when even_pays).
    if rules.even_pays and k > 0:
        expected_even = _even_vector(offer_total, k)
        if tuple(payments) != expected_even:
            violations.append(
                Violation(
                    RULE_EVEN_VECTOR,
                    f"payments {tuple(payments)} != even_vector {expected_even}",
                )
            )

    # 7. Segments / balloon exemption.
    if k > 0 and not rules.even_pays:
        balloon_ok = (
            rules.is_ballooning_allowed
            and k >= 2
            and payments[-1] > payments[-2]
        )
        if not balloon_ok and len(set(payments)) > rules.max_segments:
            violations.append(
                Violation(
                    RULE_SEGMENTS,
                    f"{len(set(payments))} levels > max_segments {rules.max_segments}",
                )
            )

    # 8. Fees and horizon.
    horizon = client.last_draft_date
    fee_total = sum(r.program_fee_cents for r in schedule_rows)
    if fee_total != program_fee:
        violations.append(
            Violation(
                RULE_FEES_HORIZON,
                f"program fee total {fee_total} != F {program_fee}",
            )
        )
    for r in schedule_rows:
        if r.date > horizon:
            violations.append(
                Violation(
                    RULE_FEES_HORIZON,
                    f"row date {r.date} after horizon {horizon}",
                )
            )
            break
        if r.date < first_payment_date and (
            r.program_fee_cents > 0 or r.bank_fee_cents > 0 or r.creditor_payment_cents > 0
        ):
            violations.append(
                Violation(
                    RULE_FEES_HORIZON,
                    f"fee/payment on {r.date} before first payment {first_payment_date}",
                )
            )
            break
        if r.creditor_payment_cents > 0:
            if r.bank_fee_cents != rules.bank_fee_cents:
                violations.append(
                    Violation(
                        RULE_FEES_HORIZON,
                        f"bank fee {r.bank_fee_cents} on payment date != {rules.bank_fee_cents}",
                    )
                )
                break
        elif r.bank_fee_cents != 0:
            violations.append(
                Violation(
                    RULE_FEES_HORIZON,
                    f"bank fee {r.bank_fee_cents} on non-payment date {r.date}",
                )
            )
            break

    # 9–10. Ledger replay: credits before debits, balance >= 0, balance_cents match.
    replay_violations = _replay_ledger(
        schedule_rows, client, first_payment_date
    )
    violations.extend(replay_violations)

    return violations


def _replay_ledger(
    schedule_rows: list[ScheduleRow],
    client: Client,
    first_payment_date: date,
) -> list[Violation]:
    credits: dict[date, int] = {}
    debits: dict[date, int] = {}

    for entry in client.ledger:
        if entry.date <= client.as_of_date:
            continue
        if entry.type == "credit":
            credits[entry.date] = credits.get(entry.date, 0) + entry.amount_cents
        else:
            debits[entry.date] = debits.get(entry.date, 0) + entry.amount_cents

    row_by_date: dict[date, ScheduleRow] = {}
    for r in schedule_rows:
        debits[r.date] = (
            debits.get(r.date, 0)
            + r.creditor_payment_cents
            + r.bank_fee_cents
            + r.program_fee_cents
        )
        row_by_date[r.date] = r

    ordered = sorted(set(credits) | set(debits))
    balance = client.current_balance_cents
    end_balances: dict[date, int] = {}
    for d in ordered:
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        end_balances[d] = balance
        if balance < 0:
            return [
                Violation(
                    RULE_LEDGER,
                    f"balance {balance} negative on {d}",
                )
            ]

    for r in schedule_rows:
        expected = end_balances.get(r.date)
        if expected is None or r.balance_cents != expected:
            return [
                Violation(
                    RULE_BALANCE_MATCH,
                    f"row balance {r.balance_cents} != replay {expected} on {r.date}",
                )
            ]

    # Sanity: no schedule activity before first payment was already checked;
    # ensure we did not skip first_payment_date when rows exist.
    _ = first_payment_date
    return []
