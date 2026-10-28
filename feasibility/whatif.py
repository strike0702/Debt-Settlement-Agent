"""What-if on creditor terms: how the affordable prices move when one term changes.

``what_if(problem, change)`` returns a ``WhatIf``: the changed ``Problem`` plus lazy
answers on the 1%..100% scan grid (``fits(bp)``, ``max_bp``, ``feasible_bps``), each
point an ``engine.is_feasible`` check. ``unlocking_changes(problem, ask_bp)`` lists the
single-term changes under which the ask fits, in the agent's order (first payment date,
then minimum payment, max payments, structure, segments), closest value first.

Reuse: the base simulation (``Problem.base``) depends on the client, the cadence and
the bank fee only, so a change to the minimum payment, the payment count, the structure
or the segment limit keeps it and recomputes only ``k_max`` and the floors. A new first
payment date changes the cadence, so it rebuilds the ``Problem``. Feasibility does not
depend on the objective, fee policy or cushion (docs/ENGINE.md), so neither do these.

``candidates(problem, field)`` is the scan grid per term (the same grid
``app.agent.term_alts`` has always used); the ranking among candidates (closest value,
highest ceiling) is the caller's policy and stays in ``term_alts``. No scoring, no
schedules here: ``engine.evaluate`` builds the plan once a term is agreed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from feasibility.engine import is_feasible
from feasibility.models import CreditorRules, add_months, end_of_month
from feasibility.problem import Problem, build_problem
from feasibility.shapes import compute_floors

Field = Literal[
    "first_payment_date", "min_payment_cents", "max_payments", "payment_structure",
    "max_segments",
]
FIELDS: tuple[Field, ...] = (
    "first_payment_date", "min_payment_cents", "max_payments", "payment_structure",
    "max_segments",
)
SCAN_BPS: tuple[int, ...] = tuple(range(100, 10001, 100))  # the affordability grid

# Scan limits (the term_alts grid): ~2 years of month ends; $10 steps down to $10;
# +1 payment up to 60; at most 20 values per numeric term; +2 levels at most.
MAX_DATE_CANDIDATES = 24
MIN_PAYMENT_STEP_CENTS = 1000
MIN_PAYMENT_FLOOR_CENTS = 1000
MAX_PAYMENTS_CEILING = 60
MAX_NUMERIC_CANDIDATES = 20
MAX_EXTRA_SEGMENTS = 2

# payment_structure value -> (even_pays, is_ballooning_allowed); same map as the adapter.
STRUCTURE_FLAGS: dict[str, tuple[bool, bool]] = {
    "even": (True, False),
    "balloon": (False, True),
    "flexible": (False, False),
}


@dataclass(frozen=True)
class TermChange:
    """One creditor term set to a new value (``date`` for the first payment date)."""

    field: Field
    value: date | int | str


class WhatIf:
    """Affordability of ``problem`` after ``change``; every answer is computed on demand."""

    def __init__(self, change: TermChange, problem: Problem) -> None:
        self.change = change
        self.problem = problem
        self._fits: dict[int, bool] = {}
        self._max_bp: int | None = None
        self._max_done = False
        self._cap = total_cap(problem)

    def fits(self, bp: int) -> bool:
        """Is some plan feasible at ``bp`` under the changed terms?"""
        ok = self._fits.get(bp)
        if ok is None:
            ok = self.problem.offer_total(bp) <= self._cap and is_feasible(self.problem, bp)
            self._fits[bp] = ok
        return ok

    @property
    def max_bp(self) -> int | None:
        """Highest feasible grid point (scanned from 100% down; the set need not be a band)."""
        if not self._max_done:
            self._max_bp = next((bp for bp in reversed(SCAN_BPS) if self.fits(bp)), None)
            self._max_done = True
        return self._max_bp

    @property
    def feasible_bps(self) -> list[int]:
        """Every feasible grid point, ascending (same set as the adapter's scan)."""
        return [bp for bp in SCAN_BPS if self.fits(bp)]


def total_cap(problem: Problem) -> int:
    """Largest offer total S that passes the tail test for some k (-1 when none does).

    Necessary for feasibility: after the k-th payment every window has lost S plus k
    bank fees, so ``S <= min(base_w[k..M]) - bf*k`` and the last window must still hold
    F (``search.Search._phi_tail``). A larger S is infeasible with no search, which
    lets ``max_bp`` skip the top of the grid.
    """
    if problem.M == 0 or problem.k_max == 0 or problem.pre_min < 0:
        return -1
    bw, bf, M = problem.base_w, problem.rules.bank_fee_cents, problem.M
    cap, run = -1, bw[M] - problem.program_fee_cents
    suffix = [0] * (M + 2)
    suffix[M + 1] = run
    for j in range(M, 0, -1):
        run = min(run, bw[j])
        suffix[j] = run
    for k in range(1, problem.k_max + 1):
        cap = max(cap, suffix[k] - bf * k)
    return cap


def structure_of(rules: CreditorRules) -> str:
    """``even`` / ``balloon`` / ``flexible`` from the rule flags."""
    if rules.even_pays:
        return "even"
    return "balloon" if rules.is_ballooning_allowed else "flexible"


def changed_rules(rules: CreditorRules, change: TermChange) -> CreditorRules:
    """``rules`` with one term replaced (not valid for ``first_payment_date``)."""
    v = change.value
    if change.field == "min_payment_cents":
        return replace(rules, min_payment_cents=int(v))  # type: ignore[arg-type]
    if change.field == "max_payments":
        n = int(v)  # type: ignore[arg-type]
        # Keep "no token limit" (token pays tracking max_payments) when it was so.
        token = n if rules.max_token_pays == rules.max_payments else rules.max_token_pays
        return replace(rules, max_payments=n, max_terms=n, max_token_pays=token)
    if change.field == "payment_structure":
        even, balloon = STRUCTURE_FLAGS[str(v)]
        return replace(rules, even_pays=even, is_ballooning_allowed=balloon)
    if change.field == "max_segments":
        return replace(rules, max_segments=int(v))  # type: ignore[arg-type]
    raise ValueError(f"not a rules change: {change.field!r}")


def changed_problem(problem: Problem, change: TermChange) -> Problem:
    """The ``Problem`` after ``change``; keeps the base simulation unless the date moves."""
    if change.field == "first_payment_date":
        if not isinstance(change.value, date):
            raise TypeError("first_payment_date needs a date")
        return build_problem(
            problem.client,
            problem.rules,
            change.value,
            creditor_balance_cents=problem.creditor_balance_cents,
            original_balance_cents=problem.original_balance_cents,
            fee_policy=problem.fee_policy,
            min_cushion_cents=problem.min_cushion_cents,
        )
    rules = changed_rules(problem.rules, change)
    M = problem.M
    k_max = min(rules.max_payments, rules.max_terms, M) if M > 0 else 0
    return replace(
        problem, rules=rules, k_max=k_max, floors=compute_floors(rules, max(k_max, 1))
    )


def what_if(problem: Problem, change: TermChange) -> WhatIf:
    """Affordability under one changed term."""
    return WhatIf(change, changed_problem(problem, change))


def candidates(problem: Problem, field: Field) -> list[TermChange]:
    """The values worth trying for ``field``, in scan order (may include the current date).

    Dates: month ends from the later of today and the first draft, through the last
    draft. Minimum payment: $10 steps down to $10. Max payments: +1 up to 60. Structure:
    the other two. Segments: up to two more levels, staircase rules only.
    """
    rules, client = problem.rules, problem.client
    out: list[TermChange] = []
    if field == "first_payment_date":
        cur = end_of_month(max(client.as_of_date, client.first_draft_date))
        for _ in range(MAX_DATE_CANDIDATES):
            if cur > client.last_draft_date:
                break
            out.append(TermChange(field, cur))
            nxt = end_of_month(add_months(cur, 1))
            if nxt <= cur:
                break
            cur = nxt
    elif field == "min_payment_cents":
        v = rules.min_payment_cents - MIN_PAYMENT_STEP_CENTS
        while v >= MIN_PAYMENT_FLOOR_CENTS and len(out) < MAX_NUMERIC_CANDIDATES:
            out.append(TermChange(field, v))
            v -= MIN_PAYMENT_STEP_CENTS
    elif field == "max_payments":
        n = rules.max_payments + 1
        while n <= MAX_PAYMENTS_CEILING and len(out) < MAX_NUMERIC_CANDIDATES:
            out.append(TermChange(field, n))
            n += 1
    elif field == "payment_structure":
        now = structure_of(rules)
        out = [TermChange(field, s) for s in STRUCTURE_FLAGS if s != now]
    elif field == "max_segments":
        if not rules.even_pays:
            top = min(problem.k_max, rules.max_segments + MAX_EXTRA_SEGMENTS)
            out = [TermChange(field, s) for s in range(rules.max_segments + 1, top + 1)]
    else:
        raise ValueError(f"unknown field: {field!r}")
    return out


def unlocking_changes(problem: Problem, ask_bp: int) -> list[WhatIf]:
    """Single-term changes under which ``ask_bp`` fits, field by field, closest first.

    Fields in the agent's order (first payment date, min payment, max payments,
    structure, segments). Dates are ordered by distance from the current first payment
    date; the current value of a term is never listed.
    """
    out: list[WhatIf] = []
    for field in FIELDS:
        changes = candidates(problem, field)
        if field == "first_payment_date":
            fpd = problem.first_payment_date
            changes = sorted(
                (c for c in changes if c.value != fpd),
                key=lambda c: abs((c.value - fpd).days),  # type: ignore[operator]
            )
        for change in changes:
            w = what_if(problem, change)
            if w.fits(ask_bp):
                out.append(w)
    return out
