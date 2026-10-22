"""Engine entry points: ``evaluate(problem, bp)`` and ``evaluate_offer(client, offer, rules)``.

Part 1: generate the candidate family (``shapes``), simulate each vector via the
prefix-cap shortcut on the ``Problem`` base run, place the fee (``scoring``), keep the
best ``score_key``, flatten a cliff, and assemble schedule rows. Part 2 (no feasible
candidate): minimal lump sum and monthly increment (``rescue``). ``evaluate_offer`` is
the take-home compatible wrapper (builds a ``Problem`` per call); callers that scan many
settlement % for one client (``app.adapter.engine_adapter.affordability``) build the
``Problem`` once and call ``evaluate``. All money is integer cents; settlement is bp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from feasibility.constants import BUFFER_DRAFT_PCT, DEFAULT_BUFFER_CENTS
from feasibility.models import Client, CreditorRules, Offer
from feasibility.problem import Problem, build_problem
from feasibility.rescue import compute_increment, compute_lump
from feasibility.scoring import (
    FeePlacement,
    evaluate_candidate,
    flatten_cliff,
    score_key,
)
from feasibility.shapes import Candidate, generate_candidates
from feasibility.simulate import SimulationResult
from feasibility.util import round_half_up


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


def _search_feasible(
    client: Client,
    candidates: list[Candidate],
    cadence: list[date],
    bank_fee_cents: int,
    F: int,
    buffer: int,
    S: int,
    k_max: int,
    floors: list[int],
    rules: CreditorRules,
    base: SimulationResult | None = None,
) -> tuple[Candidate, FeePlacement, SimulationResult] | None:
    """Return (cand, placement, sim) for the best feasible candidate, or None."""
    M = len(cadence)
    best = None
    best_key = None
    for cand in candidates:
        result = evaluate_candidate(client, cand, cadence, bank_fee_cents, F, buffer, base)
        if result is None:
            continue
        placement, sim = result
        key = score_key(cand, placement, sim, M)
        if best_key is None or key > best_key:
            best_key = key
            best = (cand, placement, sim)

    if best is None:
        return None

    cand, placement, sim = best
    cand, placement, sim = flatten_cliff(
        client,
        cand,
        cadence,
        bank_fee_cents,
        F,
        buffer,
        S,
        k_max,
        floors,
        rules,
        placement,
        sim,
        base,
    )
    return cand, placement, sim


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


def evaluate(problem: Problem, bp: int) -> Result:
    """Evaluate a settlement of ``bp`` basis points against a prebuilt ``Problem``."""
    S = problem.offer_total(bp)
    k_max = problem.k_max
    candidates = generate_candidates(S, k_max, problem.floors, problem.rules) if k_max > 0 else []
    return evaluate_candidates(problem, S, candidates)


def evaluate_candidates(problem: Problem, S: int, candidates: list[Candidate]) -> Result:
    """Score a given candidate family for offer total ``S`` (Part 1, else Part 2).

    ``evaluate`` passes ``shapes.generate_candidates``; ``legacy.legacy_evaluate``
    passes the frozen take-home family so the gap report can compare generators.
    """
    client = problem.client
    rules = problem.rules
    cadence = problem.cadence
    base = problem.base
    F = problem.program_fee_cents
    k_max = problem.k_max
    floors = problem.floors

    buffer = max(
        DEFAULT_BUFFER_CENTS,
        round_half_up(BUFFER_DRAFT_PCT, client.draft_amount_cents),
    )

    # Tier C: try with soft buffer, fall back to 0.
    found = _search_feasible(
        client, candidates, cadence, rules.bank_fee_cents, F, buffer, S, k_max, floors, rules,
        base,
    )
    if found is None and buffer > 0:
        found = _search_feasible(
            client, candidates, cadence, rules.bank_fee_cents, F, 0, S, k_max, floors, rules,
            base,
        )

    if found is not None:
        cand, placement, sim = found
        rows = _assemble_schedule(cand, cadence, placement, sim, rules.bank_fee_cents)
        return Result(
            feasible=True,
            pay_shape_used=cand.label,
            schedule=rows,
            additional_funds=None,
        )

    # Part 2 — only issued from a buffer=0 evaluation (we already are here).
    lump = compute_lump(client, candidates, cadence, rules.bank_fee_cents, F, S, base)
    incr = compute_increment(client, candidates, cadence, rules.bank_fee_cents, F, base)
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
