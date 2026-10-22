"""Part 2 rescue: minimal lump sum and minimal monthly draft increment.

Runs only when no candidate is feasible. For each candidate vector the lump is
closed form (largest overdraft, or the fee shortfall on the last window) and the
increment is the largest ``ceil(shortfall / drafts landed by then)``; the answer
is the minimum over the candidate family from ``shapes.generate_candidates``, so
it is exact only relative to that family (see the Phase 55 gap report). Guardrail
caps come from ``constants.py``. Called by ``engine.py``; returns plain results
that the engine wraps as ``FundsOption``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from feasibility.constants import (
    GUARDRAIL_INCREMENT_FLOOR_CENTS,
    GUARDRAIL_INCREMENT_PCT,
    GUARDRAIL_LUMP_SUM_PCT,
)
from feasibility.models import Client
from feasibility.shapes import Candidate
from feasibility.simulate import SimulationResult, apply_payments, simulate
from feasibility.util import ceil_div, round_half_up


def _simulate(
    client: Client,
    cand: Candidate,
    cadence: list[date],
    bank_fee_cents: int,
    base: SimulationResult | None,
) -> SimulationResult:
    if base is not None:
        return apply_payments(base, cadence, cand.payments, bank_fee_cents)
    return simulate(client, list(zip(cadence[: cand.k], cand.payments)), cadence, bank_fee_cents)


@dataclass(frozen=True)
class LumpResult:
    """Minimal single extra credit and the latest date it can safely land."""
    amount_cents: int
    date: date | None
    within_guardrail: bool
    reason: str


@dataclass(frozen=True)
class IncrementResult:
    """Minimal uniform addition to every future draft (``num_drafts`` of them)."""
    amount_cents: int
    num_drafts: int
    within_guardrail: bool
    reason: str


def _cents_to_dollars(cents: int) -> str:
    """Reason-text rendering only (never a money path): integer split, no float."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"${sign}{whole}.{frac:02d}"


def _structural_unfixable_reason() -> str:
    return (
        "no additional funding of this form can create a valid schedule: "
        "creditor minimum-payment rules cannot be satisfied within the "
        "horizon for any payment count"
    )


def compute_lump(
    client: Client,
    candidates: list[Candidate],
    cadence: list[date],
    bank_fee_cents: int,
    F: int,
    S: int,
    base: SimulationResult | None = None,
) -> LumpResult:
    """Global min lump L* and latest-safe placement date."""
    if not candidates or not cadence:
        return LumpResult(
            amount_cents=0,
            date=None,
            within_guardrail=False,
            reason=_structural_unfixable_reason(),
        )

    t_M = cadence[-1]
    best: tuple[int, int, tuple[int, ...], date] | None = None

    for cand in candidates:
        sim = _simulate(client, cand, cadence, bank_fee_cents, base)
        max_overdraft = max(((-b) for b in sim.balances.values()), default=0)
        fee_short = F - sim.w[len(cadence)]
        L = max(0, max_overdraft, fee_short)

        violations: list[date] = []
        for d, b in sim.balances.items():
            if b < 0:
                violations.append(d)
            elif d >= t_M and b < F:
                violations.append(d)
        if violations:
            t_late = min(violations)
        else:
            t_late = min(
                (e.date for e in client.ledger if e.date > client.as_of_date),
                default=t_M,
            )

        cand_key = (L, cand.k, cand.payments)
        if best is None or cand_key < (best[0], best[1], best[2]):
            best = (L, cand.k, cand.payments, t_late)

    assert best is not None
    L_star, _, _, t_late = best
    cap = round_half_up(GUARDRAIL_LUMP_SUM_PCT, S)
    within = L_star <= cap
    reason = ""
    if not within:
        reason = (
            f"a lump sum of {_cents_to_dollars(L_star)} would be required, exceeding "
            f"the affordability cap of {_cents_to_dollars(cap)} (65% of the settlement "
            f"total); we do not recommend asks above this level"
        )
    return LumpResult(
        amount_cents=L_star,
        date=t_late,
        within_guardrail=within,
        reason=reason,
    )


def compute_increment(
    client: Client,
    candidates: list[Candidate],
    cadence: list[date],
    bank_fee_cents: int,
    F: int,
    base: SimulationResult | None = None,
) -> IncrementResult:
    """Global min uniform monthly draft increment X*."""
    future_drafts = [
        e for e in client.ledger if e.type == "credit" and e.date > client.as_of_date
    ]
    N = len(future_drafts)
    draft_dates = sorted(e.date for e in future_drafts)

    def d_count(v: date) -> int:
        return sum(1 for dd in draft_dates if dd <= v)

    if not candidates or not cadence:
        return IncrementResult(
            amount_cents=0,
            num_drafts=N,
            within_guardrail=False,
            reason=_structural_unfixable_reason(),
        )

    t_M = cadence[-1]
    best_X: int | None = None
    any_fixable = False
    saw_pre_draft_shortfall = False

    for cand in candidates:
        sim = _simulate(client, cand, cadence, bank_fee_cents, base)
        required = 0
        unfixable = False
        for v, b in sim.balances.items():
            if b < 0:
                dv = d_count(v)
                if dv == 0:
                    unfixable = True
                    saw_pre_draft_shortfall = True
                    break
                required = max(required, ceil_div(-b, dv))
            # Fee collectibility on the tail window — independent of overdraft
            # (when B < 0 and v >= t_M, need ceil((F-B)/d), not just ceil((-B)/d)).
            if v >= t_M and b < F:
                dv = d_count(v)
                if dv == 0:
                    unfixable = True
                    saw_pre_draft_shortfall = True
                    break
                required = max(required, ceil_div(F - b, dv))
        if unfixable:
            continue
        any_fixable = True
        if best_X is None or required < best_X:
            best_X = required

    cap = max(
        GUARDRAIL_INCREMENT_FLOOR_CENTS,
        round_half_up(GUARDRAIL_INCREMENT_PCT, client.draft_amount_cents),
    )

    if not any_fixable:
        cause = (
            "shortfall occurs before any future draft lands"
            if saw_pre_draft_shortfall
            else "creditor minimum-payment rules cannot be satisfied within the "
            "horizon for any payment count"
        )
        return IncrementResult(
            amount_cents=0,
            num_drafts=N,
            within_guardrail=False,
            reason=f"no additional funding of this form can create a valid schedule: {cause}",
        )

    assert best_X is not None
    within = best_X <= cap
    reason = ""
    if not within:
        reason = (
            f"a monthly increase of {_cents_to_dollars(best_X)} would be required, "
            f"exceeding the affordability cap of {_cents_to_dollars(cap)} "
            f"(the greater of $100 and 40% of the monthly draft)"
        )
    return IncrementResult(
        amount_cents=best_X,
        num_drafts=N,
        within_guardrail=within,
        reason=reason,
    )
