"""Why a settlement % has no payment plan: one reason code, the date, the shortfall.

``explain(problem, bp, objective)`` answers the question the affordability curve
cannot: *why* is this price infeasible, and by how much. It returns ``None`` when a
plan exists, else an ``Infeasibility`` with the first reason that applies, in order:

- ``horizon``: no payment date fits before the client's savings plan ends (M = 0).
- ``floors``: the offer total is below the creditor's minimum payment, so no legal
  payment vector exists at all; the shortfall is ``f_1 - S``.
- ``segments``: legal vectors exist but none fits, and one more payment level
  (``max_segments + 1``, plain staircases only) would make a plan fit.
- ``cash_short``: the client's balance would go below zero.
- ``fee_short``: balances stay at or above zero but the last window cannot cover the
  program fee.

For ``segments`` / ``cash_short`` / ``fee_short`` the shortfall is the exact minimal
lump ``L*`` over every legal vector (``search.Search.min_lump``, the same number as the
rescue lump), ``closest_k`` is the payment count of the (L, k, payments)-smallest vector
that needs only ``L*``, and ``binding_date`` is that vector's first break date. Adding
``L*`` as a credit on ``binding_date`` makes that vector feasible: every earlier date
already held, and end-of-day balances on and after it rise by ``L*``
(``tests/engine/test_explain.py`` checks this against the brute-force oracle).
Cash vs fee: ``cash_short`` when the lump is set by an overdraft, ``fee_short`` when
it is set by the fee on the last window.

Feasibility does not depend on the objective, the fee policy or the soft cushion
(docs/ENGINE.md), so neither does the answer; ``objective`` is validated and kept in
the signature so callers pass the same mode they evaluate with. Everything here is
PRIVATE (client finances): the agent shows it on the operator trace only.
Called by ``app.agent.orchestrator`` when the rep's ask is above the curve.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from feasibility.objectives import FEE_FIRST, check_objective
from feasibility.problem import Problem
from feasibility.rescue import _latest_date
from feasibility.search import Search

Reason = Literal["horizon", "floors", "segments", "cash_short", "fee_short"]
REASONS: tuple[Reason, ...] = ("horizon", "floors", "segments", "cash_short", "fee_short")


@dataclass(frozen=True)
class Infeasibility:
    """Why ``bp`` has no plan. Amounts are integer cents; PRIVATE throughout."""

    reason: Reason
    bp: int
    binding_date: date | None  # latest date the shortfall can land (first break date)
    shortfall_cents: int | None  # None for ``horizon``
    closest_k: int | None  # payment count that needs the least; None for ``horizon``


def explain(problem: Problem, bp: int, objective: str = FEE_FIRST) -> Infeasibility | None:
    """The reason ``bp`` is infeasible on ``problem``, or ``None`` when a plan exists."""
    check_objective(objective)
    if problem.M == 0 or problem.k_max == 0:
        return Infeasibility("horizon", bp, problem.first_payment_date, None, None)
    S = problem.offer_total(bp)
    search = Search(problem, S)
    if search.any_feasible(0):
        return None
    found = search.min_lump()
    if found is None:
        # No legal vector of any length. k = 1 is legal whenever S >= f_1, so this is
        # the minimum payment (floors only grow with k).
        return Infeasibility(
            "floors", bp, problem.cadence[0], max(problem.floors[1] - S, 0) or None, 1
        )
    L, vec = found
    sim = problem.simulate_vector(vec)
    F = problem.program_fee_cents
    when = _latest_date(problem.client, sim, problem.cadence, F)
    overdraft = max(0, -min(sim.balances.values(), default=0))
    reason: Reason = "cash_short" if overdraft >= L else "fee_short"
    if _more_segments_fit(problem, S):
        reason = "segments"
    return Infeasibility(reason, bp, when, L, len(vec))


def _more_segments_fit(problem: Problem, S: int) -> bool:
    """Would one more payment level (plain staircase rules) give a feasible plan?"""
    rules = problem.rules
    if rules.even_pays or rules.max_segments >= problem.k_max:
        return False
    # Segments change neither floors, k_max nor the base simulation.
    wider = replace(problem, rules=replace(rules, max_segments=rules.max_segments + 1))
    return Search(wider, S).any_feasible(0)
