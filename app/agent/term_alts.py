"""Non-price alternatives: which term change would make the rep's ask affordable.

When no payment plan fits the client's savings at the rep's ask, the agent
offers one term change at a time instead of a lower price, in a fixed order:
a later first payment date, then a lower minimum payment, then more payments
(``find_next_term_alt``). Each ``find_alt_*`` scans candidate values through
the feasibility engine: the closest value at which the ask fits, else the one
with the highest ceiling, else ``None``. Alternatives already offered (``terms_countered`` keys
from ``app.agent.policy``) are skipped. Called by the orchestrator while it
builds the engine context for ``decide``; no LLM, no state changes.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import date
from typing import Any

from app.adapter.engine_adapter import (
    affordability,
)
from app.agent.policy import (
    field_already_countered,
    terms_counter_key,
)
from app.domain.scenario import CallScenario
from feasibility.models import (
    CreditorRules,
    add_months,
    end_of_month,
)


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
    client = scenario.client
    start = end_of_month(max(client.as_of_date, client.first_draft_date))
    end = client.last_draft_date
    if start > end:
        return None

    candidates: list[date] = []
    cur = start
    # Cap scans (~24 months) so a bad fixture cannot hang the turn.
    for _ in range(24):
        if cur > end:
            break
        candidates.append(cur)
        nxt = end_of_month(add_months(cur, 1))
        if nxt <= cur:
            break
        cur = nxt

    best: date | None = None
    best_rank: tuple[int, int, int] | None = None
    for cand in candidates:
        iso = cand.isoformat()
        if iso in already:
            continue
        aff = affordability(scenario, rules, cand)
        if aff.max_bp is None:
            continue
        ask_ok = ask_bp is None or ask_bp in aff.feasible_bps
        dist = abs((cand - requested).days)
        # Ask-feasible: closest date. Otherwise a later month with a much
        # higher ceiling must beat the current month's token curve.
        if ask_ok:
            rank = (0, dist, -aff.max_bp)
        else:
            rank = (1, -aff.max_bp, dist)
        if best is None or best_rank is None or rank < best_rank:
            best = cand
            best_rank = rank
    return best


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
    floor = 1000
    if current <= floor:
        return None

    best: int | None = None
    # ask_ok → (0, distance, -max_bp); else → (1, -max_bp, distance)
    best_rank: tuple[int, int, int] | None = None
    cand = current - 1000
    scanned = 0
    while cand >= floor and scanned < 20:
        trial = dc_replace(rules, min_payment_cents=cand)
        aff = affordability(scenario, trial, fpd)
        if aff.max_bp is not None:
            ask_ok = ask_bp is None or ask_bp in aff.feasible_bps
            improved = baseline_max_bp is None or aff.max_bp > baseline_max_bp
            if ask_ok or improved:
                dist = current - cand
                if ask_ok:
                    rank = (0, dist, -aff.max_bp)
                else:
                    rank = (1, -aff.max_bp, dist)
                if best is None or best_rank is None or rank < best_rank:
                    best = cand
                    best_rank = rank
                    # Closest ask-ok wins; scanning high→low so first is enough.
                    if ask_ok:
                        return best
        cand -= 1000
        scanned += 1
    return best


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
    ceiling = 60
    if current >= ceiling:
        return None

    best: int | None = None
    # ask_ok → (0, distance, -max_bp); else → (1, -max_bp, distance)
    best_rank: tuple[int, int, int] | None = None
    cand = current + 1
    scanned = 0
    while cand <= ceiling and scanned < 20:
        # Keep "no token limit" semantics when token pays tracked max_payments.
        new_token = (
            cand if rules.max_token_pays == rules.max_payments else rules.max_token_pays
        )
        trial = dc_replace(
            rules,
            max_payments=cand,
            max_terms=cand,
            max_token_pays=new_token,
        )
        aff = affordability(scenario, trial, fpd)
        if aff.max_bp is not None:
            ask_ok = ask_bp is None or ask_bp in aff.feasible_bps
            improved = baseline_max_bp is None or aff.max_bp > baseline_max_bp
            if ask_ok or improved:
                dist = cand - current
                if ask_ok:
                    rank = (0, dist, -aff.max_bp)
                else:
                    rank = (1, -aff.max_bp, dist)
                if best is None or best_rank is None or rank < best_rank:
                    best = cand
                    best_rank = rank
                    # Lowest ask-ok wins; scanning low→high so first is enough.
                    if ask_ok:
                        return best
        cand += 1
        scanned += 1
    return best


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
    a prior alt only unlocked a ceiling still below the ask.
    """
    if not field_already_countered(terms_countered, "first_payment_date"):
        already_iso = {
            k.split(":", 1)[1]
            for k in terms_countered
            if k.startswith("first_payment_date:")
        }
        alt = find_alt_first_payment_date(
            scenario,
            rules,
            ask_bp=ask_bp,
            requested=fpd,
            already=already_iso,
        )
        if alt is not None and alt != fpd:
            return ("first_payment_date", alt)

    alt_min = find_alt_min_payment_cents(
        scenario,
        rules,
        ask_bp=ask_bp,
        fpd=fpd,
        current=min_payment_cents,
        baseline_max_bp=baseline_max_bp,
    )
    if alt_min is not None and alt_min < min_payment_cents:
        key = terms_counter_key("min_payment_cents", alt_min)
        if key not in terms_countered:
            return ("min_payment_cents", alt_min)

    alt_max = find_alt_max_payments(
        scenario,
        rules,
        ask_bp=ask_bp,
        fpd=fpd,
        current=max_payments,
        baseline_max_bp=baseline_max_bp,
    )
    if alt_max is not None and alt_max > max_payments:
        key = terms_counter_key("max_payments", alt_max)
        if key not in terms_countered:
            return ("max_payments", alt_max)

    return None
