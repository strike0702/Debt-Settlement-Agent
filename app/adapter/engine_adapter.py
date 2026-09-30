"""Bridge between belief/scenario and the vendored feasibility engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any, Protocol

from app.domain.belief import BeliefState, TermStatus
from app.domain.facts import Fact, FactSet
from app.domain.fields import FIELD_REGISTRY, REQUIRED_FIELDS
from app.domain.scenario import CallScenario
from app.domain.units import bp_to_decimal
from feasibility.engine import AdditionalFunds, Result, ScheduleRow, evaluate_offer
from feasibility.models import CreditorRules, Offer
from feasibility.util import round_half_up


class FirmFees(Protocol):
    program_fee_pct: float
    bank_fee_cents: int


class NeedsInfo(Exception):
    """Raised when belief is not ready to build CreditorRules."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        super().__init__(f"needs info for: {', '.join(fields)}")


@dataclass(frozen=True)
class EvalSummary:
    feasible: bool
    shape: str | None
    rows: list[ScheduleRow] | None
    additional_funds: AdditionalFunds | None
    assumed_fields: list[str]
    facts: FactSet
    offer_total_cents: int
    program_fee_cents: int


@dataclass(frozen=True)
class Affordability:
    max_bp: int | None
    feasible_bps: list[int]
    curve: tuple[bool, ...]  # aligned with range(100, 10001, 100)


_STRUCTURE_TO_FLAGS: dict[str, tuple[bool, bool]] = {
    # payment_structure -> (even_pays, is_ballooning_allowed)
    "even": (True, False),
    "balloon": (False, True),
    "flexible": (False, False),
}


def build_rules(belief: BeliefState, firm: FirmFees) -> CreditorRules:
    """Map belief + firm fees into engine CreditorRules.

    Raises NeedsInfo if any required field is not usable (KNOWN/ASSUMED).
    """
    missing = [
        name
        for name in REQUIRED_FIELDS
        if not belief.usable_for_engine(name)
    ]
    # Also block if any registered field we need is TENTATIVE/CONTRADICTED
    # while required; missing_required already covers UNKNOWN/TENTATIVE/CONTRADICTED.
    if missing:
        raise NeedsInfo(missing)

    max_payments = int(belief.get("max_payments").value)
    min_payment_cents = int(belief.get("min_payment_cents").value)
    structure = str(belief.get("payment_structure").value)
    if structure not in _STRUCTURE_TO_FLAGS:
        raise ValueError(f"unknown payment_structure: {structure!r}")
    even_pays, balloon = _STRUCTURE_TO_FLAGS[structure]

    max_segments_term = belief.get("max_segments")
    max_segments = (
        int(max_segments_term.value)
        if belief.usable_for_engine("max_segments") and max_segments_term.value is not None
        else 2
    )

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
        program_fee_pct=float(firm.program_fee_pct),
    )


def assumed_fields(belief: BeliefState) -> list[str]:
    return [
        name
        for name in (spec.name for spec in FIELD_REGISTRY)
        if belief.get(name).status == TermStatus.ASSUMED
    ]


def evaluate(
    scenario: CallScenario,
    rules: CreditorRules,
    bp: int,
    first_payment_date: date,
    *,
    assumed: list[str] | None = None,
) -> EvalSummary:
    """Run the engine at settlement `bp` and tag facts PUBLIC/PRIVATE."""
    settlement = bp_to_decimal(bp)
    offer = Offer(
        creditor=scenario.creditor,
        creditor_balance_cents=scenario.creditor_balance_cents,
        original_balance_cents=scenario.original_balance_cents,
        settlement_pct=float(settlement),
        first_payment_date=first_payment_date,
    )
    result = evaluate_offer(scenario.client, offer, rules)
    offer_total = round_half_up(settlement, scenario.creditor_balance_cents)
    program_fee = round_half_up(rules.program_fee_pct, scenario.original_balance_cents)
    facts = _facts_from_result(result, offer_total, first_payment_date)
    return EvalSummary(
        feasible=result.feasible,
        shape=result.pay_shape_used,
        rows=result.schedule,
        additional_funds=result.additional_funds,
        assumed_fields=list(assumed or []),
        facts=facts,
        offer_total_cents=offer_total,
        program_fee_cents=program_fee,
    )


def _facts_from_result(
    result: Result,
    offer_total: int,
    first_payment_date: date,
) -> FactSet:
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
        float(rules.program_fee_pct),
    )


def _scenario_key(scenario: CallScenario) -> tuple[Any, ...]:
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
) -> Affordability:
    """Scan settlement bps with no monotonicity assumption. Cached."""
    return _affordability_cached(_scenario_key(scenario), _rules_tuple(rules), fpd.isoformat())


@lru_cache(maxsize=256)
def _affordability_cached(
    scenario_key: tuple[Any, ...],
    rules_tuple: tuple[Any, ...],
    fpd_iso: str,
) -> Affordability:
    scenario = _scenario_from_key(scenario_key)
    rules = _rules_from_tuple(rules_tuple)
    fpd = date.fromisoformat(fpd_iso)
    curve: list[bool] = []
    feasible_bps: list[int] = []
    for bp in range(100, 10001, 100):
        summary = evaluate(scenario, rules, bp, fpd)
        ok = summary.feasible
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
        program_fee_pct=float(t[9]),
    )


def _scenario_from_key(key: tuple[Any, ...]) -> CallScenario:
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
    _affordability_cached.cache_clear()
