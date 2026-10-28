"""Bridge between belief/scenario and the feasibility engine (owned v2).

Builds ``CreditorRules`` from usable belief fields (raises ``NeedsInfo`` when
required terms are missing), runs ``evaluate`` / ``affordability``, and tags
returned amounts as PUBLIC or PRIVATE ``Fact``s. Settlement percentages are
integer **basis points** (bp): 100 bp = 1%, 4500 bp = 45%, 10000 bp = 100%,
passed to the engine as ``Offer.settlement_bp``. The firm's fractional
``program_fee_pct`` is converted to ``CreditorRules.program_fee_bp`` exactly once,
in ``build_rules`` (values that are not whole bp are rejected).
Never use float for money math; cents and bp stay ints end-to-end.

Which legal plan the engine returns follows ``ENGINE_OBJECTIVE`` and
``ENGINE_FEE_POLICY`` (``app.config``; defaults ``fee_first`` / ``front_loaded``,
docs/ENGINE.md), unless a caller passes them. Both are part of the affordability
cache key. ``feasibility.engine.evaluate_offer`` (the take-home wrapper) is not used
here: it is always ``fee_first`` + ``front_loaded``.

``scenario.min_cushion_cents`` (firm default, or the call's override) is the engine's
soft client cushion: ``evaluate`` reports ``cushion_kept`` and tags the cushion as a
PRIVATE fact (blocklisted, never in NLG). The affordability scan ignores it: the
cushion never changes which prices are affordable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any, Protocol

from app.config import get_settings
from app.domain.belief import BeliefState, TermStatus
from app.domain.facts import Fact, FactSet
from app.domain.fields import FIELD_REGISTRY, REQUIRED_FIELDS
from app.domain.scenario import CallScenario
from app.domain.units import bp_to_decimal
from feasibility.engine import AdditionalFunds, Result, ScheduleRow, is_feasible
from feasibility.engine import evaluate as engine_evaluate
from feasibility.models import CreditorRules
from feasibility.problem import Problem, build_problem
from feasibility.util import fraction_to_bp, round_half_up


class FirmFees(Protocol):
    """Anything with firm fee fields (e.g. CallScenario) works as `firm`."""

    program_fee_pct: float
    bank_fee_cents: int


class NeedsInfo(Exception):
    """Belief not ready: a required creditor-rule field is missing/weak/conflicted."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        super().__init__(f"needs info for: {', '.join(fields)}")


@dataclass(frozen=True)
class EvalSummary:
    """One engine run, plus FactSet already tagged PUBLIC vs PRIVATE for NLG."""

    feasible: bool
    shape: str | None  # "even" | "staircase" | "balloon" when feasible
    rows: list[ScheduleRow] | None
    additional_funds: AdditionalFunds | None  # rescue options when infeasible
    assumed_fields: list[str]
    facts: FactSet
    offer_total_cents: int
    program_fee_cents: int
    # Soft client cushion used for this run (0 = none) and whether the plan kept it.
    min_cushion_cents: int = 0
    cushion_kept: bool | None = None


@dataclass(frozen=True)
class Affordability:
    """Result of scanning settlement % from 1%..100% in 1% steps (100..10000 bp)."""

    max_bp: int | None  # highest feasible bp, or None if nothing works
    feasible_bps: list[int]
    # Index i <-> bp = (i+1)*100. Feasibility is NOT assumed monotonic.
    curve: tuple[bool, ...]


# Belief payment_structure -> engine flags (even_pays, is_ballooning_allowed).
_STRUCTURE_TO_FLAGS: dict[str, tuple[bool, bool]] = {
    "even": (True, False),
    "balloon": (False, True),
    "flexible": (False, False),
}


def build_rules(belief: BeliefState, firm: FirmFees) -> CreditorRules:
    """Map belief + firm fees into engine CreditorRules.

    Only KNOWN/ASSUMED fields are usable. Raises NeedsInfo if any *required*
    field (max_payments, min_payment_cents, payment_structure) is not usable.
    Firm fees are never asked of the rep — they come from firm.json. Raises
    ``ValueError`` if ``firm.program_fee_pct`` is not a whole basis point.
    """
    missing = [
        name
        for name in REQUIRED_FIELDS
        if not belief.usable_for_engine(name)
    ]
    if missing:
        raise NeedsInfo(missing)

    max_payments = int(belief.get("max_payments").value)
    min_payment_cents = int(belief.get("min_payment_cents").value)
    structure = str(belief.get("payment_structure").value)
    if structure not in _STRUCTURE_TO_FLAGS:
        raise ValueError(f"unknown payment_structure: {structure!r}")
    even_pays, balloon = _STRUCTURE_TO_FLAGS[structure]

    # Optional fields: keep ASSUMED defaults when still usable.
    max_segments_term = belief.get("max_segments")
    max_segments = (
        int(max_segments_term.value)
        if belief.usable_for_engine("max_segments") and max_segments_term.value is not None
        else 2
    )

    # "No token limit" == max_token_pays equals max_payments.
    token_term = belief.get("max_token_pays")
    if belief.usable_for_engine("max_token_pays") and token_term.value is not None:
        max_token_pays = int(token_term.value)
    else:
        max_token_pays = max_payments

    tiers_term = belief.get("min_payment_tiers")
    if belief.usable_for_engine("min_payment_tiers") and tiers_term.value is not None:
        min_payment_tiers = list(tiers_term.value)
    else:
        min_payment_tiers = []

    # Engine treats max_terms and max_payments as redundant; we set both equal.
    return CreditorRules(
        max_terms=max_payments,
        max_payments=max_payments,
        min_payment_cents=min_payment_cents,
        max_token_pays=max_token_pays,
        min_payment_tiers=min_payment_tiers,
        even_pays=even_pays,
        is_ballooning_allowed=balloon,
        max_segments=max_segments,
        bank_fee_cents=int(firm.bank_fee_cents),
        program_fee_bp=fraction_to_bp(firm.program_fee_pct),
    )


def engine_mode(
    objective: str | None = None, fee_policy: str | None = None
) -> tuple[str, str]:
    """(objective, fee policy): the given values, else the ``ENGINE_*`` settings."""
    settings = get_settings()
    return (
        objective or settings.engine_objective,
        fee_policy or settings.engine_fee_policy,
    )


def assumed_fields(belief: BeliefState) -> list[str]:
    """Field names still on ASSUMED defaults (for audit / wrap-up disclosure)."""
    return [
        name
        for name in (spec.name for spec in FIELD_REGISTRY)
        if belief.get(name).status == TermStatus.ASSUMED
    ]


def scenario_problem(
    scenario: CallScenario,
    rules: CreditorRules,
    first_payment_date: date,
    *,
    fee_policy: str | None = None,
) -> Problem:
    """The engine ``Problem`` for this call's client, rules and date (engine fee policy).

    For callers that ask several questions of one client and rules (``explain``,
    ``what_if``) without rebuilding the base simulation each time.
    """
    _, fee_policy = engine_mode(None, fee_policy)
    return build_problem(
        scenario.client,
        rules,
        first_payment_date,
        creditor_balance_cents=scenario.creditor_balance_cents,
        original_balance_cents=scenario.original_balance_cents,
        fee_policy=fee_policy,
        min_cushion_cents=scenario.min_cushion_cents,
    )


def evaluate(
    scenario: CallScenario,
    rules: CreditorRules,
    bp: int,
    first_payment_date: date,
    *,
    assumed: list[str] | None = None,
    objective: str | None = None,
    fee_policy: str | None = None,
) -> EvalSummary:
    """Run the engine at one settlement percentage (`bp` basis points).

    Builds a ``Problem`` under the engine mode (``engine_mode``), evaluates it, and
    wraps the result with a FactSet whose visibility marks each fact speakable or
    client-private.
    """
    objective, fee_policy = engine_mode(objective, fee_policy)
    problem = scenario_problem(scenario, rules, first_payment_date, fee_policy=fee_policy)
    result = engine_evaluate(problem, bp, objective)
    # Same Decimal half-up as the engine, so Fact values match its money rules.
    offer_total = round_half_up(bp_to_decimal(bp), scenario.creditor_balance_cents)
    program_fee = round_half_up(
        bp_to_decimal(rules.program_fee_bp), scenario.original_balance_cents
    )
    facts = _facts_from_result(result, offer_total, first_payment_date)
    if scenario.min_cushion_cents > 0:
        # Client-protection policy: PRIVATE, so it is blocklisted and never reaches NLG.
        facts.add(
            Fact(
                id="min_cushion",
                kind="money",
                value=scenario.min_cushion_cents,
                visibility="PRIVATE",
                source="engine",
            )
        )
    return EvalSummary(
        feasible=result.feasible,
        shape=result.pay_shape_used,
        rows=result.schedule,
        additional_funds=result.additional_funds,
        assumed_fields=list(assumed or []),
        facts=facts,
        offer_total_cents=offer_total,
        program_fee_cents=program_fee,
        min_cushion_cents=scenario.min_cushion_cents,
        cushion_kept=result.cushion_kept,
    )


def _facts_from_result(
    result: Result,
    offer_total: int,
    first_payment_date: date,
) -> FactSet:
    """Tag speakable schedule numbers PUBLIC; balances/fees/rescue PRIVATE."""
    facts = FactSet()
    facts.add(
        Fact(
            id="offer_total",
            kind="money",
            value=offer_total,
            visibility="PUBLIC",
            source="engine",
        )
    )
    facts.add(
        Fact(
            id="first_payment_date",
            kind="date",
            value=first_payment_date,
            visibility="PUBLIC",
            source="creditor",
        )
    )

    if result.schedule:
        pay_rows = [r for r in result.schedule if r.creditor_payment_cents > 0]
        payments = [r.creditor_payment_cents for r in pay_rows]
        facts.add(
            Fact(
                id="num_payments",
                kind="count",
                value=len(payments),
                visibility="PUBLIC",
                source="engine",
            )
        )
        if payments:
            facts.add(
                Fact(
                    id="last_payment",
                    kind="money",
                    value=payments[-1],
                    visibility="PUBLIC",
                    source="engine",
                )
            )
            # Distinct levels in first-seen order (for "starting at X rising to Y").
            seen: list[int] = []
            for p in payments:
                if p not in seen:
                    seen.append(p)
            for i, level in enumerate(seen):
                facts.add(
                    Fact(
                        id=f"payment_level_{i}",
                        kind="money",
                        value=level,
                        visibility="PUBLIC",
                        source="engine",
                    )
                )

        # Per-row balances and fees must never reach the NLG prompt.
        for i, row in enumerate(result.schedule):
            facts.add(
                Fact(
                    id=f"balance_{i}",
                    kind="money",
                    value=row.balance_cents,
                    visibility="PRIVATE",
                    source="engine",
                )
            )
            facts.add(
                Fact(
                    id=f"program_fee_{i}",
                    kind="money",
                    value=row.program_fee_cents,
                    visibility="PRIVATE",
                    source="engine",
                )
            )
            facts.add(
                Fact(
                    id=f"bank_fee_{i}",
                    kind="money",
                    value=row.bank_fee_cents,
                    visibility="PRIVATE",
                    source="engine",
                )
            )
        fee_total = sum(r.program_fee_cents for r in result.schedule)
        facts.add(
            Fact(
                id="program_fee",
                kind="money",
                value=fee_total,
                visibility="PRIVATE",
                source="engine",
            )
        )

    # Rescue lump / increment are walk-away math — never spoken.
    if result.additional_funds is not None:
        facts.add(
            Fact(
                id="rescue_lump",
                kind="money",
                value=result.additional_funds.lump_sum.amount_cents,
                visibility="PRIVATE",
                source="engine",
            )
        )
        facts.add(
            Fact(
                id="rescue_increment",
                kind="money",
                value=result.additional_funds.monthly_increment.amount_cents,
                visibility="PRIVATE",
                source="engine",
            )
        )

    return facts


def _rules_tuple(rules: CreditorRules) -> tuple[Any, ...]:
    """Hashable CreditorRules for lru_cache keys (all ints/bools; fee in bp)."""
    tiers = tuple((int(a), int(b)) for a, b in rules.min_payment_tiers)
    return (
        rules.max_terms,
        rules.max_payments,
        rules.min_payment_cents,
        rules.max_token_pays,
        tiers,
        rules.even_pays,
        rules.is_ballooning_allowed,
        rules.max_segments,
        rules.bank_fee_cents,
        rules.program_fee_bp,
    )


def _scenario_key(scenario: CallScenario) -> tuple[Any, ...]:
    """Hashable snapshot of scenario inputs (client ledger included)."""
    client = scenario.client
    ledger = tuple(
        (e.date.isoformat(), e.amount_cents, e.type) for e in client.ledger
    )
    return (
        scenario.id,
        scenario.creditor,
        scenario.creditor_balance_cents,
        scenario.original_balance_cents,
        scenario.program_fee_pct,
        scenario.bank_fee_cents,
        client.draft_amount_cents,
        client.draft_day,
        client.first_draft_date.isoformat(),
        client.last_draft_date.isoformat(),
        client.as_of_date.isoformat(),
        client.current_balance_cents,
        ledger,
    )


def affordability(
    scenario: CallScenario,
    rules: CreditorRules,
    fpd: date,
    *,
    objective: str | None = None,
    fee_policy: str | None = None,
) -> Affordability:
    """Scan every 100 bp from 100..10000; cache by (scenario, rules, fpd, engine mode).

    Builds one engine ``Problem`` (cadence, floors, base simulation) per scan and
    reuses it for all 100 points. Callers on the async path should wrap with ``asyncio.to_thread``.
    Do not assume the feasible set is a single contiguous band. Feasibility does not
    depend on the objective or fee policy (docs/ENGINE.md), so each point is a
    feasibility check (``engine.is_feasible``), not a ranked evaluate. Both are still
    in the key, so a cached scan always matches the mode that produced it.
    """
    objective, fee_policy = engine_mode(objective, fee_policy)
    return _affordability_cached(
        _scenario_key(scenario), _rules_tuple(rules), fpd.isoformat(), objective, fee_policy
    )


@lru_cache(maxsize=256)
def _affordability_cached(
    scenario_key: tuple[Any, ...],
    rules_tuple: tuple[Any, ...],
    fpd_iso: str,
    objective: str,
    fee_policy: str,
) -> Affordability:
    # Rebuild objects from the hashable key, then evaluate each grid point.
    scenario = _scenario_from_key(scenario_key)
    rules = _rules_from_tuple(rules_tuple)
    fpd = date.fromisoformat(fpd_iso)
    problem = build_problem(
        scenario.client,
        rules,
        fpd,
        creditor_balance_cents=scenario.creditor_balance_cents,
        original_balance_cents=scenario.original_balance_cents,
        fee_policy=fee_policy,
    )
    curve: list[bool] = []
    feasible_bps: list[int] = []
    for bp in range(100, 10001, 100):  # 1%, 2%, ..., 100%
        ok = is_feasible(problem, bp)  # same answer as engine_evaluate(...).feasible
        curve.append(ok)
        if ok:
            feasible_bps.append(bp)
    max_bp = feasible_bps[-1] if feasible_bps else None
    return Affordability(
        max_bp=max_bp,
        feasible_bps=feasible_bps,
        curve=tuple(curve),
    )


def _rules_from_tuple(t: tuple[Any, ...]) -> CreditorRules:
    return CreditorRules(
        max_terms=int(t[0]),
        max_payments=int(t[1]),
        min_payment_cents=int(t[2]),
        max_token_pays=int(t[3]),
        min_payment_tiers=[(int(a), int(b)) for a, b in t[4]],
        even_pays=bool(t[5]),
        is_ballooning_allowed=bool(t[6]),
        max_segments=int(t[7]),
        bank_fee_cents=int(t[8]),
        program_fee_bp=int(t[9]),
    )


def _scenario_from_key(key: tuple[Any, ...]) -> CallScenario:
    """Inverse of ``_scenario_key`` for cache hits."""
    from feasibility.models import Client, LedgerEntry

    (
        sid,
        creditor,
        creditor_balance_cents,
        original_balance_cents,
        program_fee_pct,
        bank_fee_cents,
        draft_amount_cents,
        draft_day,
        first_draft_date,
        last_draft_date,
        as_of_date,
        current_balance_cents,
        ledger,
    ) = key
    client = Client(
        draft_amount_cents=int(draft_amount_cents),
        draft_day=int(draft_day),
        first_draft_date=date.fromisoformat(first_draft_date),
        last_draft_date=date.fromisoformat(last_draft_date),
        as_of_date=date.fromisoformat(as_of_date),
        current_balance_cents=int(current_balance_cents),
        ledger=[
            LedgerEntry(date.fromisoformat(d), int(amt), typ)  # type: ignore[arg-type]
            for d, amt, typ in ledger
        ],
    )
    return CallScenario(
        id=str(sid),
        client=client,
        creditor=str(creditor),
        creditor_balance_cents=int(creditor_balance_cents),
        original_balance_cents=int(original_balance_cents),
        program_fee_pct=float(program_fee_pct),
        bank_fee_cents=int(bank_fee_cents),
    )


def clear_affordability_cache() -> None:
    """Drop cached scans (tests / rules change mid-process)."""
    _affordability_cached.cache_clear()
