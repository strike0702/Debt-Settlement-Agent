"""Part 2 rescue: minimal lump sum and minimal monthly draft increment.

Runs only when no vector is feasible. For one vector the lump is closed form (largest
overdraft, or the fee shortfall on the last window) and the increment is the largest
``ceil(shortfall / drafts landed by then)``. ``exact_lump`` / ``exact_increment``
(Phase 57b, what ``engine.evaluate`` uses) take the minimum over every legal vector via
``search.Search`` (exact: both are monotone in prefix sums, so the dominating family is
enough and no binary search is needed). ``latest_date`` is the lump-date rule (first
break date of the (L, k, payments)-smallest vector), shared with ``explain.py``. The
guardrail caps come from ``constants.py``. Returns plain results that the engine wraps
as ``FundsOption``.
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
from feasibility.problem import Problem
from feasibility.search import Search
from feasibility.simulate import SimulationResult
from feasibility.util import round_half_up


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


def latest_date(client: Client, sim: SimulationResult, cadence: list[date], F: int) -> date:
    """First date the chosen vector breaks (overdraft, or fee short on/after ``t_M``)."""
    t_M = cadence[-1]
    violations: list[date] = []
    for d, b in sim.balances.items():
        if b < 0:
            violations.append(d)
        elif d >= t_M and b < F:
            violations.append(d)
    if violations:
        return min(violations)
    return min((e.date for e in client.ledger if e.date > client.as_of_date), default=t_M)


def _lump_result(L_star: int, t_late: date, S: int) -> LumpResult:
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


def exact_lump(problem: Problem, search: Search) -> LumpResult:
    """L* over every legal vector (``search.min_lump``) with the same date and guardrail."""
    found = search.min_lump()
    if found is None or not problem.cadence:
        return LumpResult(
            amount_cents=0,
            date=None,
            within_guardrail=False,
            reason=_structural_unfixable_reason(),
        )
    L_star, vec = found
    sim = problem.simulate_vector(vec)
    t_late = latest_date(problem.client, sim, problem.cadence, problem.program_fee_cents)
    return _lump_result(L_star, t_late, search.S)


def _increment_result(
    best_X: int | None, N: int, saw_pre_draft_shortfall: bool, draft_cents: int
) -> IncrementResult:
    cap = max(
        GUARDRAIL_INCREMENT_FLOOR_CENTS,
        round_half_up(GUARDRAIL_INCREMENT_PCT, draft_cents),
    )

    if best_X is None:
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


def exact_increment(problem: Problem, search: Search) -> IncrementResult:
    """X* over every legal vector (``search.min_increment``), same texts and guardrail.

    A legal vector is unfixable only when a shortfall lands before any draft, so when
    every one is unfixable the cause is the pre-draft one.
    """
    client = problem.client
    N = sum(1 for e in client.ledger if e.type == "credit" and e.date > client.as_of_date)
    X, any_legal = search.min_increment()
    if not any_legal or not problem.cadence:
        return IncrementResult(
            amount_cents=0,
            num_drafts=N,
            within_guardrail=False,
            reason=_structural_unfixable_reason(),
        )
    return _increment_result(X, N, True, client.draft_amount_cents)
