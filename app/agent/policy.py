"""Pure synchronous negotiation policy (PLAN §6.2).

``decide`` maps belief + TurnAnalysis + Affordability → an ``Action``. No I/O
and no LLM: unit tests cover every numbered rule. Spoken figures for an action
live in ``Action.facts`` (PUBLIC only); side effects apply on speech ack via
``Action.effects``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from math import ceil
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.adapter.engine_adapter import Affordability
from app.agent.nlu_types import TurnAnalysis
from app.config import Settings, get_settings
from app.domain.belief import BeliefState
from app.domain.facts import Fact
from app.domain.fields import FIELDS_BY_NAME
from app.store.audit import AuditLog
from feasibility.engine import ScheduleRow


class Phase(StrEnum):
    OPENING = "OPENING"
    DISCOVERY = "DISCOVERY"
    NEGOTIATE = "NEGOTIATE"
    CONFIRM = "CONFIRM"
    WRAP = "WRAP"
    ESCALATE = "ESCALATE"
    END = "END"


class Intent(StrEnum):
    OPENING = "OPENING"
    ASK = "ASK"
    ASK_SETTLEMENT = "ASK_SETTLEMENT"
    READ_BACK = "READ_BACK"
    CLARIFY = "CLARIFY"
    REFUSE_PRIVATE = "REFUSE_PRIVATE"
    REFUSE_COMMIT = "REFUSE_COMMIT"
    COUNTER = "COUNTER"
    CONFIRM_SCHEDULE = "CONFIRM_SCHEDULE"
    PROPOSE_WRAP = "PROPOSE_WRAP"
    NO_DEAL_WRAP = "NO_DEAL_WRAP"
    ESCALATE = "ESCALATE"


class Effect(BaseModel):
    """Side effect committed only when spoken sentences are acked."""

    kind: Literal[
        "offer_counter",
        "set_pending_readback",
        "clear_pending_readback",
        "inc_private_ask",
        "inc_commit_demand",
        "record_ask",
        "inc_reject_at_max",
        "set_phase",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass
class NegotiationState:
    """Mutable bargain bookkeeping for one call (ask ladder, counters, flags)."""

    ask_bp: int | None = None
    ask_history: list[int] = field(default_factory=list)
    counters_offered: list[int] = field(default_factory=list)
    rejects: int = 0  # rejections while our last counter was at max_bp
    private_ask_count: int = 0
    commit_demand_count: int = 0
    pending_readback: str | None = None
    phase: Phase = Phase.OPENING
    turn_idx: int = 0


class Action(BaseModel):
    intent: Intent
    facts: dict[str, Fact] = Field(default_factory=dict)
    # Non-numeric fills (ask copy, escalate reason, firm name). Never digits.
    text_slots: dict[str, str] = Field(default_factory=dict)
    required: set[str] = Field(default_factory=set)
    effects: list[Effect] = Field(default_factory=list)
    next_phase: Phase
    # Optional free-form reason for ESCALATE / tests.
    reason: str | None = None


class Agreement(BaseModel):
    creditor: str
    bp: int
    offer_total: int
    rows: list[dict[str, Any]]
    assumed_fields: list[str]
    status: Literal["pending_client_approval"] = "pending_client_approval"


def ask_pct_to_bp(pct: float) -> int:
    """Convert settlement ask percent points (45.0 → 4500 bp)."""
    return int(
        (Decimal(str(pct)) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP)
    )


def next_counter(
    *,
    ask_bp: int,
    max_bp: int,
    feasible_bps: list[int],
    c_prev: int | None,
    anchor_ratio: float,
    concession_factor: float,
) -> int:
    """Ladder toward ``min(ask, max)``; snap down to feasible; never >= ask."""
    if not feasible_bps:
        raise ValueError("next_counter requires at least one feasible bp")

    target = min(ask_bp, max_bp)
    ratio_cap = int(Decimal(str(anchor_ratio)) * Decimal(target))
    under_ratio = [bp for bp in feasible_bps if bp <= ratio_cap]
    anchor = max(under_ratio) if under_ratio else min(feasible_bps)

    if c_prev is None:
        c_next = anchor
    else:
        delta = target - c_prev
        step = ceil(delta * concession_factor) if delta > 0 else 0
        raw = c_prev + step
        under = [bp for bp in feasible_bps if bp <= raw]
        c_next = max(under) if under else c_prev
        if c_next < c_prev:
            c_next = c_prev

    # Never at or above the rep's ask.
    if c_next >= ask_bp:
        under_ask = [bp for bp in feasible_bps if bp < ask_bp]
        if not under_ask:
            # No legal counter below ask — caller should NO_DEAL / escalate.
            return c_prev if c_prev is not None else min(feasible_bps)
        c_next = max(under_ask)
        if c_prev is not None and c_next < c_prev:
            c_next = c_prev

    # Also never above max_bp.
    if c_next > max_bp:
        under_max = [bp for bp in feasible_bps if bp <= max_bp and bp < ask_bp]
        c_next = max(under_max) if under_max else c_next

    return c_next


def draft_agreement(
    *,
    creditor: str,
    bp: int,
    offer_total: int,
    rows: list[ScheduleRow] | list[dict[str, Any]],
    assumed_fields: list[str],
    audit: AuditLog | None = None,
    call_id: str | None = None,
) -> Agreement:
    """Build a pending client-approval agreement; never a spoken commitment."""
    serialized: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            serialized.append(row)
        else:
            serialized.append(
                {
                    "date": row.date.isoformat(),
                    "creditor_payment_cents": row.creditor_payment_cents,
                    "program_fee_cents": row.program_fee_cents,
                    "bank_fee_cents": row.bank_fee_cents,
                    "balance_cents": row.balance_cents,
                }
            )
    agreement = Agreement(
        creditor=creditor,
        bp=bp,
        offer_total=offer_total,
        rows=serialized,
        assumed_fields=list(assumed_fields),
        status="pending_client_approval",
    )
    if audit is not None and call_id is not None:
        audit.append(
            call_id,
            "agent",
            "agreement_drafted",
            agreement.model_dump(),
        )
    return agreement


def _fact_for_value(fact_id: str, field: str, value: Any) -> Fact:
    """Build a PUBLIC fact for clarify/read-back spoken values."""
    spec = FIELDS_BY_NAME[field]
    if spec.kind == "cents":
        return Fact(
            id=fact_id, kind="money", value=int(value), visibility="PUBLIC", source="creditor"
        )
    if spec.kind == "date":
        assert isinstance(value, date)
        return Fact(
            id=fact_id, kind="date", value=value, visibility="PUBLIC", source="creditor"
        )
    if spec.kind == "int":
        return Fact(
            id=fact_id, kind="count", value=int(value), visibility="PUBLIC", source="creditor"
        )
    # enum / tiers — speak as count only when int-like; else text_slots
    if isinstance(value, int):
        return Fact(
            id=fact_id, kind="count", value=value, visibility="PUBLIC", source="creditor"
        )
    # Fallback count 0 unused; caller should use text_slots for enums.
    return Fact(
        id=fact_id, kind="count", value=0, visibility="PUBLIC", source="creditor"
    )


def decide(
    belief: BeliefState,
    neg: NegotiationState,
    analysis: TurnAnalysis,
    afford: Affordability | None,
    *,
    settings: Settings | None = None,
    rescue_within_guardrail: bool = False,
    confirm_facts: dict[str, Fact] | None = None,
    counter_offer_total_cents: int | None = None,
) -> Action:
    """Apply §6.2 rules in order; return the next agent ``Action``."""
    cfg = settings or get_settings()

    # Track ask from this turn's analysis (orchestrator also persists via effects).
    ask_bp = neg.ask_bp
    effects: list[Effect] = []
    if analysis.settlement_ask_pct is not None:
        ask_bp = ask_pct_to_bp(analysis.settlement_ask_pct)
        effects.append(Effect(kind="record_ask", data={"bp": ask_bp}))

    # 1. Turn cap
    if neg.turn_idx > cfg.max_turns:
        return Action(
            intent=Intent.NO_DEAL_WRAP,
            effects=effects,
            next_phase=Phase.END,
            reason="max_turns",
        )

    # 2. Hostility
    if analysis.hostility >= cfg.hostility_threshold:
        return Action(
            intent=Intent.ESCALATE,
            text_slots={"escalate_reason": "The tone on this call needs a specialist."},
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value})],
            next_phase=Phase.ESCALATE,
            reason="hostile",
        )

    # 3. Private-info ask
    if analysis.asks_client_private_info:
        if neg.private_ask_count >= 1:
            return Action(
                intent=Intent.ESCALATE,
                text_slots={
                    "escalate_reason": "I need to hand this off after a sensitive request."
                },
                effects=effects
                + [
                    Effect(kind="inc_private_ask"),
                    Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value}),
                ],
                next_phase=Phase.ESCALATE,
                reason="sensitive_request",
            )
        return Action(
            intent=Intent.REFUSE_PRIVATE,
            effects=effects + [Effect(kind="inc_private_ask")],
            next_phase=neg.phase if neg.phase != Phase.OPENING else Phase.DISCOVERY,
            reason="private_info",
        )

    # 4. Commitment demand
    if analysis.demands_commitment:
        if neg.commit_demand_count >= 1:
            return Action(
                intent=Intent.ESCALATE,
                text_slots={
                    "escalate_reason": "I need to involve someone about a commitment demand."
                },
                effects=effects
                + [
                    Effect(kind="inc_commit_demand"),
                    Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value}),
                ],
                next_phase=Phase.ESCALATE,
                reason="commitment_demand",
            )
        return Action(
            intent=Intent.REFUSE_COMMIT,
            effects=effects + [Effect(kind="inc_commit_demand")],
            next_phase=neg.phase if neg.phase != Phase.OPENING else Phase.DISCOVERY,
            reason="commitment",
        )

    # 10 (early): In CONFIRM, accept → wrap with draft intent.
    if neg.phase == Phase.CONFIRM and (
        analysis.stance == "accept" or analysis.readback_response == "confirm"
    ):
        facts = dict(confirm_facts or {})
        return Action(
            intent=Intent.PROPOSE_WRAP,
            facts=facts,
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.WRAP.value})],
            next_phase=Phase.WRAP,
            reason="confirmed",
        )

    # 5. Contradictions before read-backs / asks
    contradicted = belief.contradicted_fields()
    if contradicted:
        fname = contradicted[0]
        term = belief.get(fname)
        old_val = term.history[-1] if term.history else None
        new_val = term.value
        facts: dict[str, Fact] = {}
        text_slots: dict[str, str] = {"field": fname}
        required: set[str] = set()
        if old_val is not None and FIELDS_BY_NAME[fname].kind in ("cents", "int", "date"):
            facts["clarify_old"] = _fact_for_value("clarify_old", fname, old_val)
            required.add("clarify_old")
        else:
            text_slots["clarify_old"] = str(old_val)
        if new_val is not None and FIELDS_BY_NAME[fname].kind in ("cents", "int", "date"):
            facts["clarify_new"] = _fact_for_value("clarify_new", fname, new_val)
            required.add("clarify_new")
        else:
            text_slots["clarify_new"] = str(new_val)
        return Action(
            intent=Intent.CLARIFY,
            facts=facts,
            text_slots=text_slots,
            required=required,
            effects=effects,
            next_phase=Phase.DISCOVERY,
            reason=fname,
        )

    # 6. Tentative → read-back
    tentative = belief.tentative_fields()
    if tentative:
        fname = tentative[0]
        term = belief.get(fname)
        facts = {}
        text_slots = {"field": fname}
        required = set()
        if term.value is not None and FIELDS_BY_NAME[fname].kind in ("cents", "int", "date"):
            facts["readback_value"] = _fact_for_value("readback_value", fname, term.value)
            required.add("readback_value")
        else:
            text_slots["readback_value"] = str(term.value)
        return Action(
            intent=Intent.READ_BACK,
            facts=facts,
            text_slots=text_slots,
            required=required,
            effects=effects
            + [Effect(kind="set_pending_readback", data={"field": fname})],
            next_phase=Phase.DISCOVERY,
            reason=fname,
        )

    # 7. Missing required field
    missing = belief.missing_required()
    if missing:
        fname = missing[0]
        spec = FIELDS_BY_NAME[fname]
        return Action(
            intent=Intent.ASK,
            text_slots={"ask_text": spec.ask_text, "field": fname},
            required=set(),
            effects=effects,
            next_phase=Phase.DISCOVERY,
            reason=fname,
        )

    # 8. Settlement ask unknown
    if ask_bp is None:
        return Action(
            intent=Intent.ASK_SETTLEMENT,
            effects=effects,
            next_phase=Phase.DISCOVERY,
        )

    # 9. Affordability / counter / confirm
    if afford is None:
        # Rules not buildable yet — should have been caught by missing fields.
        return Action(
            intent=Intent.ASK_SETTLEMENT,
            effects=effects,
            next_phase=Phase.DISCOVERY,
        )

    if afford.max_bp is None:
        # Nothing feasible: rescue check (amounts never spoken).
        if rescue_within_guardrail:
            return Action(
                intent=Intent.ESCALATE,
                text_slots={
                    "escalate_reason": (
                        "This needs client approval for extra funds before we continue."
                    )
                },
                effects=effects
                + [Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value})],
                next_phase=Phase.ESCALATE,
                reason="out_of_guardrail",
            )
        return Action(
            intent=Intent.NO_DEAL_WRAP,
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
            next_phase=Phase.END,
            reason="infeasible",
        )

    # Rep accepted our last counter → confirm that bp.
    if analysis.stance == "accept" and neg.counters_offered:
        bp = neg.counters_offered[-1]
        facts = dict(confirm_facts or {})
        facts.setdefault(
            "settlement_pct",
            Fact(
                id="settlement_pct",
                kind="pct",
                value=bp,
                visibility="PUBLIC",
                source="engine",
            ),
        )
        return Action(
            intent=Intent.CONFIRM_SCHEDULE,
            facts=facts,
            required=set(facts.keys()) & {"offer_total", "num_payments", "first_payment_date"},
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.CONFIRM.value})],
            next_phase=Phase.CONFIRM,
            reason=f"bp={bp}",
        )

    # Ask is affordable and on the feasible grid → confirm schedule at ask.
    if ask_bp <= afford.max_bp and ask_bp in afford.feasible_bps:
        facts = dict(confirm_facts or {})
        facts.setdefault(
            "settlement_pct",
            Fact(
                id="settlement_pct",
                kind="pct",
                value=ask_bp,
                visibility="PUBLIC",
                source="engine",
            ),
        )
        return Action(
            intent=Intent.CONFIRM_SCHEDULE,
            facts=facts,
            required=set(),
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.CONFIRM.value})],
            next_phase=Phase.CONFIRM,
            reason=f"bp={ask_bp}",
        )

    # Counter ladder. Stop after MAX_COUNTERS rejections at max_bp.
    at_max = bool(neg.counters_offered) and neg.counters_offered[-1] >= afford.max_bp
    if at_max and neg.rejects >= cfg.max_counters:
        return Action(
            intent=Intent.NO_DEAL_WRAP,
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
            next_phase=Phase.END,
            reason="max_counters",
        )

    c_prev = neg.counters_offered[-1] if neg.counters_offered else None
    # If rep rejected and we were at max, count toward max_counters (on ack).
    if analysis.stance == "reject" and at_max:
        effects.append(Effect(kind="inc_reject_at_max"))

    c_next = next_counter(
        ask_bp=ask_bp,
        max_bp=afford.max_bp,
        feasible_bps=afford.feasible_bps,
        c_prev=c_prev,
        anchor_ratio=cfg.anchor_ratio,
        concession_factor=cfg.concession_factor,
    )

    # If we cannot move (stuck at/above ask or no progress), no-deal.
    if c_prev is not None and c_next <= c_prev and c_next >= afford.max_bp:
        if neg.rejects + (1 if analysis.stance == "reject" and at_max else 0) >= cfg.max_counters:
            return Action(
                intent=Intent.NO_DEAL_WRAP,
                effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
                next_phase=Phase.END,
                reason="max_counters",
            )

    if c_next >= ask_bp:
        return Action(
            intent=Intent.NO_DEAL_WRAP,
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
            next_phase=Phase.END,
            reason="no_counter_below_ask",
        )

    facts = {
        "counter_pct": Fact(
            id="counter_pct",
            kind="pct",
            value=c_next,
            visibility="PUBLIC",
            source="engine",
        ),
    }
    if counter_offer_total_cents is not None:
        facts["offer_total"] = Fact(
            id="offer_total",
            kind="money",
            value=counter_offer_total_cents,
            visibility="PUBLIC",
            source="engine",
        )

    return Action(
        intent=Intent.COUNTER,
        facts=facts,
        required={"counter_pct"},
        effects=effects
        + [
            Effect(kind="offer_counter", data={"bp": c_next}),
            Effect(kind="set_phase", data={"phase": Phase.NEGOTIATE.value}),
        ],
        next_phase=Phase.NEGOTIATE,
        reason=f"bp={c_next}",
    )


def opening_action(
    *,
    settings: Settings | None = None,
    firm_name: str | None = None,
    opening_disclosure: str | None = None,
) -> Action:
    """Deterministic OPENING line (session.start); not part of decide rules."""
    cfg = settings or get_settings()
    return Action(
        intent=Intent.OPENING,
        text_slots={
            "firm_name": firm_name or cfg.firm_name,
            "opening_disclosure": opening_disclosure or cfg.opening_disclosure,
        },
        next_phase=Phase.DISCOVERY,
    )
