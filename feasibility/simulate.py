"""Ledger simulation: full chronological replay and the prefix-cap shortcut.

``simulate`` replays committed future ledger entries plus creditor payments and
bank fees (credits before debits on each date) and reports end-of-day balances,
per-cadence window minima ``w`` and the pre-window minimum. It never places the
program fee; that is ``scoring.place_fee``. ``apply_payments`` derives the same
``SimulationResult`` from a no-payment base run (``problem.build_problem``) by
subtracting prefix payments and bank fees, which is how ``engine.evaluate`` avoids
re-simulating every candidate. ``tests/engine/test_prefix_cap.py`` proves they agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from feasibility.models import Client


@dataclass(frozen=True)
class SimulationResult:
    """Balances after committed ledger + creditor payments + bank fees (no program fee).

    ``balances`` maps every evaluated date to end-of-day balance.
    ``w`` is 1-indexed by cadence: ``w[j]`` = min balance on ``[t_j, t_{j+1})``.
    ``pre_min`` is the min balance on dates strictly before ``t_1`` (or +inf if none).
    """

    balances: dict[date, int]
    ordered_dates: list[date]
    w: list[int]  # 1-indexed; w[0] unused
    pre_min: int


def simulate(
    client: Client,
    payments: list[tuple[date, int]],
    cadence: list[date],
    bank_fee_cents: int,
) -> SimulationResult:
    """Simulate the ledger without program fees.

    ``payments`` is ``(date, creditor_payment_cents)`` for the k payment dates.
    Bank fees are added on each payment-carrying date.
    """
    # Collect credits/debits per date from future committed ledger entries.
    credits: dict[date, int] = {}
    debits: dict[date, int] = {}

    for entry in client.ledger:
        if entry.date <= client.as_of_date:
            continue
        if entry.type == "credit":
            credits[entry.date] = credits.get(entry.date, 0) + entry.amount_cents
        else:
            debits[entry.date] = debits.get(entry.date, 0) + entry.amount_cents

    for d, p in payments:
        debits[d] = debits.get(d, 0) + p + bank_fee_cents

    event_dates = set(credits) | set(debits) | set(cadence)
    ordered = sorted(event_dates)

    balance = client.current_balance_cents
    balances: dict[date, int] = {}
    for d in ordered:
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        balances[d] = balance

    if not cadence:
        return SimulationResult(
            balances=balances,
            ordered_dates=ordered,
            w=[0],
            pre_min=min(balances.values()) if balances else 10**18,
        )

    t1 = cadence[0]
    pre_vals = [balances[d] for d in ordered if d < t1]
    pre_min = min(pre_vals) if pre_vals else 10**18

    M = len(cadence)
    w = [0] * (M + 1)
    for j in range(1, M + 1):
        left = cadence[j - 1]
        right = cadence[j] if j < M else None  # [t_j, t_{j+1})
        window_vals = [
            balances[d]
            for d in ordered
            if d >= left and (right is None or d < right)
        ]
        if not window_vals:
            prior = [balances[d] for d in ordered if d <= left]
            window_vals = [prior[-1] if prior else client.current_balance_cents]
        w[j] = min(window_vals)

    return SimulationResult(
        balances=balances,
        ordered_dates=ordered,
        w=w,
        pre_min=pre_min,
    )


def apply_payments(
    base: SimulationResult,
    cadence: list[date],
    payments: Sequence[int],
    bank_fee_cents: int,
) -> SimulationResult:
    """``simulate(client, zip(cadence, payments), cadence, bf)`` from the no-payment base.

    Prefix-cap identity: with ``c_j = P_min(j,k) + bf * min(j,k)`` (prefix sum of the
    first k payments plus their bank fees), every date in window j (``t_j <= d <
    t_{j+1}``) has ``balance(d) = base(d) - c_j``, so ``w[j] = base.w[j] - c_j``;
    dates before ``t_1`` are untouched (``pre_min`` unchanged). ``base`` must come
    from ``simulate(client, [], cadence, bf)`` with the same ``cadence``.
    """
    M = len(cadence)
    k = len(payments)
    cum = [0] * (M + 1)
    run = 0
    for j in range(1, M + 1):
        if j <= k:
            run += payments[j - 1] + bank_fee_cents
        cum[j] = run
    balances: dict[date, int] = {}
    j = 0  # number of cadence dates <= d
    base_balances = base.balances
    for d in base.ordered_dates:
        while j < M and cadence[j] <= d:
            j += 1
        balances[d] = base_balances[d] - cum[j]
    w = [0] * (M + 1)
    base_w = base.w
    for j in range(1, M + 1):
        w[j] = base_w[j] - cum[j]
    return SimulationResult(
        balances=balances,
        ordered_dates=base.ordered_dates,
        w=w,
        pre_min=base.pre_min,
    )
