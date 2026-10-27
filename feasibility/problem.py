"""Per-(client, rules, first payment date) precomputation shared by every settlement %.

``build_problem`` fixes everything that does not depend on the settlement %: the
payment cadence ``t_1..t_M``, ``k_max``, the per-position floors, the program fee
``F``, and one *base* ledger simulation with no creditor payments, no bank fees and
no program fee (its window minima ``base_w`` and pre-window minimum ``pre_min``).

Prefix-cap identity (why the base run is enough): a k-payment vector ``p`` with prefix
sums ``P_j`` only lowers balances from its payment dates onward, so in window j
(``t_j <= d < t_{j+1}``) every balance is ``base(d) - P_min(j,k) - bf * min(j,k)``.
The candidate's window minima are therefore ``base_w[j]`` minus that constant, and
feasibility / fee timing depend on ``p`` only through its prefix sums: each window
gives a fixed cap on ``P_j``. ``simulate.apply_payments`` implements this and
``tests/engine/test_prefix_cap.py`` checks it against a full replay for every
candidate on the golden corpus.

``fee_policy`` (Phase 59) says how the program fee may be collected: ``front_loaded``
(the take-home rule) or ``proportional`` (fee by ``t_j`` at most ``floor(F*P_j/S)``); see
``objectives.py`` and docs/ENGINE.md. It changes fee timing only, never feasibility.

``window_caps`` and ``window_items`` are the same caps in the form the exact search
(``search.py``) uses; they are cached on first use. Pure data: no search or scoring
here. ``engine.evaluate(problem, bp)`` consumes it; the adapter builds one per
affordability scan and reuses it for all 100 bp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import cached_property

from feasibility.models import (
    Client,
    CreditorRules,
    default_first_payment_date,
    monthly_payment_dates,
)
from feasibility.shapes import compute_floors
from feasibility.simulate import SimulationResult, apply_payments, simulate
from feasibility.util import bp_fraction, round_half_up


@dataclass(frozen=True)
class Problem:
    """Settlement-%-independent engine state; build with ``build_problem``."""

    client: Client
    rules: CreditorRules
    first_payment_date: date
    creditor_balance_cents: int
    original_balance_cents: int
    cadence: list[date]
    M: int
    k_max: int
    floors: list[int]  # 1-indexed floors[1..max(k_max, 1)]
    program_fee_cents: int  # F
    base: SimulationResult  # no creditor payments, no bank fees, no program fee
    fee_policy: str = "front_loaded"  # "front_loaded" | "proportional"

    @property
    def base_w(self) -> list[int]:
        """Base window minima, 1-indexed (``base_w[0]`` unused)."""
        return self.base.w

    @property
    def pre_min(self) -> int:
        """Base minimum balance strictly before ``t_1`` (10**18 when no such date)."""
        return self.base.pre_min

    @cached_property
    def window_caps(self) -> list[int]:
        """``base_w[j] - bf*j``, 1-indexed: window j < k is non-negative iff ``P_j <= cap``.

        Settlement-independent, so ``search.Search`` reuses it across a whole scan.
        """
        bf = self.rules.bank_fee_cents
        return [0] + [self.base_w[j] - bf * j for j in range(1, self.M + 1)]

    @cached_property
    def window_items(self) -> list[list[tuple[int, int]]]:
        """Per window (0 = before ``t_1``): ``(base balance - required, drafts landed)``.

        ``required`` is F on and after ``t_M`` (the fee must be collectable), else 0. The
        uniform increment X for a window that a vector lowers by ``c`` is the largest
        ``ceil((c - slack) / drafts)`` over its dates (``search.Search._window_x``).
        """
        client = self.client
        drafts = sorted(
            e.date for e in client.ledger if e.type == "credit" and e.date > client.as_of_date
        )
        M, cad, F = self.M, self.cadence, self.program_fee_cents
        items: list[list[tuple[int, int]]] = [[] for _ in range(M + 1)]
        j = n = 0
        for d in self.base.ordered_dates:
            while j < M and cad[j] <= d:
                j += 1
            while n < len(drafts) and drafts[n] <= d:
                n += 1
            req = F if j == M and M > 0 else 0
            items[j].append((self.base.balances[d] - req, n))
        return items

    def offer_total(self, bp: int) -> int:
        """S for a settlement of ``bp`` basis points (half-up on the creditor balance)."""
        return round_half_up(bp_fraction(bp), self.creditor_balance_cents)

    def simulate_vector(self, payments: tuple[int, ...] | list[int]) -> SimulationResult:
        """Candidate simulation via the prefix-cap identity (no full replay)."""
        return apply_payments(self.base, self.cadence, payments, self.rules.bank_fee_cents)


def build_cadence(client: Client, first_payment_date: date) -> list[date]:
    """Monthly cadence from ``first_payment_date``, truncated at the horizon."""
    t1 = first_payment_date
    horizon = client.last_draft_date
    # Upper bound: months from t1 to horizon plus slack; truncate afterwards.
    months = (horizon.year - t1.year) * 12 + (horizon.month - t1.month) + 2
    months = max(months, 1)
    dates = monthly_payment_dates(t1, months)
    return [d for d in dates if d <= horizon]


def build_problem(
    client: Client,
    rules: CreditorRules,
    first_payment_date: date | None,
    *,
    creditor_balance_cents: int,
    original_balance_cents: int,
    fee_policy: str = "front_loaded",
) -> Problem:
    """Precompute cadence, caps inputs and the base simulation for one client + rules.

    ``first_payment_date=None`` means the default (end of the first draft's month).
    The two balances are keyword-only so ``evaluate(problem, bp)`` needs nothing else.
    ``fee_policy`` is ``front_loaded`` (default) or ``proportional``.
    """
    if fee_policy not in ("front_loaded", "proportional"):
        raise ValueError(f"unknown fee_policy: {fee_policy!r}")
    t1 = first_payment_date or default_first_payment_date(client)
    cadence = build_cadence(client, t1)
    M = len(cadence)
    k_max = min(rules.max_payments, rules.max_terms, M) if M > 0 else 0
    floors = compute_floors(rules, max(k_max, 1))
    F = round_half_up(bp_fraction(rules.program_fee_bp), original_balance_cents)
    base = simulate(client, [], cadence, rules.bank_fee_cents)
    return Problem(
        client=client,
        rules=rules,
        first_payment_date=t1,
        creditor_balance_cents=creditor_balance_cents,
        original_balance_cents=original_balance_cents,
        cadence=cadence,
        M=M,
        k_max=k_max,
        floors=floors,
        program_fee_cents=F,
        base=base,
        fee_policy=fee_policy,
    )
