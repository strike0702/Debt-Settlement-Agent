"""Non-price alternatives: which term change would make the rep's ask affordable.

When no payment plan fits the client's savings at the rep's ask, the agent
offers one term change at a time instead of a lower price, in a fixed order:
a later first payment date, then a lower minimum payment, then more payments
(``find_next_term_alt``). Each ``find_alt_*`` walks the engine's candidate grid
(``feasibility.whatif.candidates``) and asks ``feasibility.whatif.what_if`` about
each value: the closest value at which the ask fits, else the one with the
highest ceiling, else ``None``. The engine reuses the base simulation for
minimum-payment and payment-count changes and rebuilds it only for a new date;
the ranking below is policy and stays here. Alternatives already offered
(``terms_countered`` keys from ``app.agent.policy``) are skipped. Called by the
orchestrator while it builds the engine context for ``decide``; no LLM, no
state changes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.adapter.engine_adapter import scenario_problem
from app.agent.policy import (
    field_already_countered,
    terms_counter_key,
)
from app.domain.scenario import CallScenario
from feasibility.models import CreditorRules
from feasibility.problem import Problem
from feasibility.whatif import Field as TermField
from feasibility.whatif import TermChange, WhatIf, candidates, changed_problem, what_if

Rank = tuple[int, int, int]


def _ask_ok(w: WhatIf, ask_bp: int | None) -> bool:
    # On the 1% scan grid, like the affordability curve: an off-grid ask never fits.
    return ask_bp is None or (ask_bp % 100 == 0 and 100 <= ask_bp <= 10000 and w.fits(ask_bp))


def _rank(ask_ok: bool, dist: int, max_bp: int) -> Rank:
    # Ask-feasible: closest value. Otherwise the highest ceiling, then closest.
    return (0, dist, -max_bp) if ask_ok else (1, -max_bp, dist)


def _alt_first_payment_date(
    problem: Problem, *, ask_bp: int | None, requested: date, already: set[str]
) -> date | None:
    best: date | None = None
    best_rank: Rank | None = None
    for change in candidates(problem, "first_payment_date"):
        cand = change.value
        assert isinstance(cand, date)
        if cand.isoformat() in already:
            continue
        w = what_if(problem, change)
        if w.max_bp is None:
            continue
        rank = _rank(_ask_ok(w, ask_bp), abs((cand - requested).days), w.max_bp)
        if best_rank is None or rank < best_rank:
            best, best_rank = cand, rank
    return best


def _alt_numeric(
    problem: Problem,
    field: TermField,
    *,
    ask_bp: int | None,
    current: int,
    baseline_max_bp: int | None,
) -> int | None:
    """Shared min-payment / max-payments walk: candidates go away from ``current``."""
    best: int | None = None
    best_rank: Rank | None = None
    for change in candidates(problem, field):
        cand = int(change.value)  # type: ignore[arg-type]
        w = what_if(problem, change)
        if w.max_bp is None:
            continue
        ask_ok = _ask_ok(w, ask_bp)
        improved = baseline_max_bp is None or w.max_bp > baseline_max_bp
        if not (ask_ok or improved):
            continue
        rank = _rank(ask_ok, abs(cand - current), w.max_bp)
        if best_rank is None or rank < best_rank:
            best, best_rank = cand, rank
            # The grid walks away from current, so the first ask-ok value is closest.
            if ask_ok:
                return best
    return best


def _at_current(problem: Problem, field: TermField, current: int) -> Problem:
    """``problem`` with ``field`` at ``current`` (the caller's belief value)."""
    if getattr(problem.rules, field) == current:
        return problem
    return changed_problem(problem, TermChange(field, current))


def find_alt_first_payment_date(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    requested: date,
    already: set[str],
) -> date | None:
    """Scan EOM candidates within the savings horizon.

    Prefers the closest date where ``ask_bp`` is feasible. When the ask
    fits nowhere, prefer the highest ceiling (then the closest date). The
    current month is usually closest and can be stuck at a token percent
    while a later month clears most of the ask. Never re-offers an ISO
    date in ``already``.
    """
    problem = scenario_problem(scenario, rules, requested)
    return _alt_first_payment_date(problem, ask_bp=ask_bp, requested=requested, already=already)


def find_alt_min_payment_cents(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    fpd: date,
    current: int,
    baseline_max_bp: int | None = None,
) -> int | None:
    """Search lower min-payment floors that unlock or improve affordability.

    Steps down by $10 (1000¢) toward the field floor (1000¢). Prefers the
    least invasive floor where ``ask_bp`` is feasible. When the ask fits
    nowhere, jump to the floor with the highest ceiling (then least
    invasive) — same idea as FPD — so we do not nickle-and-dime $50→$40→$30.

    When ``baseline_max_bp`` is set (curve already non-empty but ask still
    unreachable), only candidates that make the ask feasible or raise
    ``max_bp`` above the baseline count — avoids re-offering a useless cut.
    """
    problem = _at_current(scenario_problem(scenario, rules, fpd), "min_payment_cents", current)
    return _alt_numeric(
        problem, "min_payment_cents", ask_bp=ask_bp, current=current,
        baseline_max_bp=baseline_max_bp,
    )


def find_alt_max_payments(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    fpd: date,
    current: int,
    baseline_max_bp: int | None = None,
) -> int | None:
    """Search higher max-payment counts that unlock or improve affordability.

    Steps up by 1 toward 60. Prefers the lowest count where ``ask_bp`` is
    feasible. When the ask fits nowhere, jump to the lowest count that
    hits the best ceiling — skip useless +1 steps that still leave the
    ask unreachable (4→5 when only 6 unlocks a real ladder).

    When ``baseline_max_bp`` is set, only candidates that make the ask
    feasible or raise ``max_bp`` above the baseline count.
    """
    problem = _at_current(scenario_problem(scenario, rules, fpd), "max_payments", current)
    return _alt_numeric(
        problem, "max_payments", ask_bp=ask_bp, current=current,
        baseline_max_bp=baseline_max_bp,
    )


def find_next_term_alt(
    scenario: CallScenario,
    rules: CreditorRules,
    *,
    ask_bp: int | None,
    fpd: date,
    max_payments: int,
    min_payment_cents: int,
    terms_countered: list[str],
    baseline_max_bp: int | None = None,
) -> tuple[str, Any] | None:
    """Next staggered non-price alt: FPD → lower min → higher max_payments.

    FPD is one try per field. Min / max may progress to a better value when
    a prior alt only unlocked a ceiling still below the ask. One engine
    ``Problem`` at ``fpd`` serves all three searches.
    """
    problem = scenario_problem(scenario, rules, fpd)
    if not field_already_countered(terms_countered, "first_payment_date"):
        already_iso = {
            k.split(":", 1)[1]
            for k in terms_countered
            if k.startswith("first_payment_date:")
        }
        alt = _alt_first_payment_date(
            problem, ask_bp=ask_bp, requested=fpd, already=already_iso
        )
        if alt is not None and alt != fpd:
            return ("first_payment_date", alt)

    alt_min = _alt_numeric(
        _at_current(problem, "min_payment_cents", min_payment_cents),
        "min_payment_cents",
        ask_bp=ask_bp,
        current=min_payment_cents,
        baseline_max_bp=baseline_max_bp,
    )
    if alt_min is not None and alt_min < min_payment_cents:
        key = terms_counter_key("min_payment_cents", alt_min)
        if key not in terms_countered:
            return ("min_payment_cents", alt_min)

    alt_max = _alt_numeric(
        _at_current(problem, "max_payments", max_payments),
        "max_payments",
        ask_bp=ask_bp,
        current=max_payments,
        baseline_max_bp=baseline_max_bp,
    )
    if alt_max is not None and alt_max > max_payments:
        key = terms_counter_key("max_payments", alt_max)
        if key not in terms_countered:
            return ("max_payments", alt_max)

    return None
