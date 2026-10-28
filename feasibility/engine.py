"""Engine entry points: ``evaluate(problem, bp)`` and ``evaluate_offer(client, offer, rules)``.

Part 1: ``search.Search`` finds the best ``score_key`` over every legal vector by
branch and bound on the ``Problem``'s prefix caps (Phase 57b; the take-home winner
whenever the take-home family reaches Φ*), then the winner is simulated, its fee placed
(``scoring``), a cliff flattened, and schedule rows assembled. Part 2 (nothing
feasible): exact minimal lump sum and monthly increment (``rescue.exact_*``).
``evaluate(problem, bp, objective)`` also takes ``client_first`` (Phase 59,
``objectives.py``) and places fees under ``problem.fee_policy``. ``evaluate_offer`` is
the take-home compatible wrapper, always ``fee_first`` + ``front_loaded`` (builds a
``Problem`` per call); callers that scan many settlement % for one client
(``app.adapter.engine_adapter.affordability``) build the ``Problem`` once and call
``evaluate``. All money is integer cents; settlement is bp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from feasibility.models import Client, CreditorRules, Offer
from feasibility.objectives import (
    CLIENT_FIRST,
    FEE_FIRST,
    PROPORTIONAL,
    ClientFirst,
    check_objective,
)
from feasibility.problem import Problem, build_problem
from feasibility.rescue import (
    IncrementResult,
    LumpResult,
    exact_increment,
    exact_lump,
)
from feasibility.scoring import (
    FeePlacement,
    evaluate_candidate,
    flatten_cliff,
    proportional_cap,
)
from feasibility.search import Search
from feasibility.shapes import Candidate
from feasibility.simulate import SimulationResult


@dataclass
class ScheduleRow:
    """One cadence date carrying a creditor payment and/or program fee."""

    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    """One Part 2 rescue option with its guardrail verdict."""

    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    """Both Part 2 rescue forms (reported only when infeasible)."""

    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    """Engine output: a schedule when feasible, rescue amounts when not."""

    feasible: bool
    # One of "even", "staircase", or "balloon" — the shape your solution produced
    # (driven by the creditor flags). None when infeasible.
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None
    # Phase 59: did the plan keep ``Problem.min_cushion_cents``? None when the cushion
    # is 0 or nothing is feasible. Not part of ``to_dict`` (take-home output shape).
    cushion_kept: bool | None = None

    def to_dict(self) -> dict:
        """JSON-safe output in the take-home shape (dates as ISO strings)."""
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:

            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


def _assemble_schedule(
    cand: Candidate,
    cadence: list[date],
    placement: FeePlacement,
    sim: SimulationResult,
    bank_fee_cents: int,
) -> list[ScheduleRow]:
    k = cand.k
    M = len(cadence)
    last_fee_j = 0
    for j in range(1, M + 1):
        if placement.fee[j] > 0:
            last_fee_j = j
    j_last = max(k, last_fee_j)
    rows: list[ScheduleRow] = []
    for j in range(1, j_last + 1):
        p_j = cand.payments[j - 1] if j <= k else 0
        fee_j = placement.fee[j]
        if p_j == 0 and fee_j == 0:
            continue
        bf = bank_fee_cents if j <= k else 0
        t_j = cadence[j - 1]
        bal = sim.balances[t_j] - placement.Phi[j]
        rows.append(
            ScheduleRow(
                date=t_j,
                creditor_payment_cents=p_j,
                program_fee_cents=fee_j,
                bank_fee_cents=bf,
                balance_cents=bal,
            )
        )
    return rows


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer (take-home API): build a ``Problem``, then ``evaluate``."""
    problem = build_problem(
        client,
        rules,
        offer.first_payment_date,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
    )
    return evaluate(problem, offer.settlement_bp)


def _best(search: Search, buffer: int, objective: str) -> tuple[tuple[int, ...], str] | None:
    if objective == CLIENT_FIRST:
        found = ClientFirst(search, buffer).solve()
        return None if found is None else (found[0], found[1])
    return search.best(buffer)


def evaluate(problem: Problem, bp: int, objective: str = FEE_FIRST) -> Result:
    """Evaluate a settlement of ``bp`` basis points against a prebuilt ``Problem``.

    Exact over every legal vector (``search.Search``): the take-home winner whenever the
    take-home family already reaches Φ*, and the exact L*/X* when nothing is feasible.
    ``objective`` is ``fee_first`` (take-home ranking) or ``client_first``
    (``objectives.ClientFirst``); fee timing follows ``problem.fee_policy``. Tier C:
    the plan keeps every balance at or above ``problem.min_cushion_cents`` when some
    plan can, else the best plan at buffer 0 (``Result.cushion_kept`` says which). The cliff
    post-pass runs only for ``fee_first`` + ``front_loaded``, the take-home mode it was
    built for (client-first is already exact on the largest payment).
    """
    check_objective(objective)
    S = problem.offer_total(bp)
    search = Search(problem, S)
    client, rules = problem.client, problem.rules
    prop = problem.fee_policy == PROPORTIONAL
    # Tier C: try with the firm's soft cushion, fall back to 0.
    buffer = problem.min_cushion_cents
    found = _best(search, buffer, objective)
    kept: bool | None = True if buffer > 0 else None
    if found is None and buffer > 0:
        buffer = 0
        kept = False
        found = _best(search, 0, objective)
    if found is not None:
        vec, label = found
        cand = Candidate(k=len(vec), payments=vec, label=label)
        F = problem.program_fee_cents
        cap = proportional_cap(vec, problem.M, F) if prop else None
        scored = evaluate_candidate(
            client, cand, problem.cadence, rules.bank_fee_cents, F, buffer, problem.base, cap,
        )
        assert scored is not None  # the search only returns feasible vectors
        placement, sim = scored
        if objective == FEE_FIRST and not prop:
            cand, placement, sim = flatten_cliff(
                client, cand, problem.cadence, rules.bank_fee_cents, F,
                buffer, S, problem.k_max, problem.floors, rules, placement, sim, problem.base,
            )
        rows = _assemble_schedule(cand, problem.cadence, placement, sim, rules.bank_fee_cents)
        return Result(
            feasible=True, pay_shape_used=cand.label, schedule=rows, cushion_kept=kept
        )
    return _infeasible(exact_lump(problem, search), exact_increment(problem, search))


def is_feasible(problem: Problem, bp: int) -> bool:
    """Does ``evaluate(problem, bp, objective)`` find a schedule, for any objective?

    Feasibility does not depend on the objective or the fee policy (docs/ENGINE.md),
    and Tier C falls back to buffer 0, so this is "some legal vector is feasible at
    buffer 0". It stops at the first one: the affordability scan's fast path.
    """
    return Search(problem, problem.offer_total(bp)).any_feasible(0)


def _infeasible(lump: LumpResult, incr: IncrementResult) -> Result:
    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(
            lump_sum=FundsOption(
                amount_cents=lump.amount_cents,
                within_guardrail=lump.within_guardrail,
                reason=lump.reason,
                date=lump.date,
            ),
            monthly_increment=FundsOption(
                amount_cents=incr.amount_cents,
                within_guardrail=incr.within_guardrail,
                reason=incr.reason,
                num_drafts=incr.num_drafts,
            ),
        ),
    )
