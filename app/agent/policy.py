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
from math import ceil
from typing import Any, Literal

from pydantic import BaseModel

from app.adapter.engine_adapter import Affordability
from app.config import Settings, get_settings
from app.domain.actions import Action, Effect, Intent, Phase
from app.domain.belief import BeliefState, TermStatus
from app.domain.facts import Fact
from app.domain.fields import FIELD_REGISTRY, FIELDS_BY_NAME
from app.domain.nlu_types import TurnAnalysis
from app.store.audit import AuditLog
from feasibility.engine import ScheduleRow

# Re-export for callers that import Action/Intent/Phase from policy.
__all__ = [
    "Action",
    "Agreement",
    "Effect",
    "Intent",
    "NegotiationState",
    "Phase",
    "ask_pct_to_bp",
    "decide",
    "draft_agreement",
    "next_counter",
    "opening_action",
]


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
    # Last CONFIRM fingerprint (ask + rules that shape the schedule).
    last_confirm_key: tuple[Any, ...] | None = None
    confirm_rejects: int = 0
    assumed_asked: set[str] = field(default_factory=set)
    clarify_counts: dict[str, int] = field(default_factory=dict)
    # Non-price alternatives already offered (ISO dates for first_payment_date).
    terms_countered: list[str] = field(default_factory=list)
    # Last COUNTER_TERMS date awaiting accept/reject (ISO).
    pending_terms_alt: str | None = None


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
) -> int | None:
    """Ladder toward ``min(ask, max)``; snap down to feasible; never >= ask.

    Returns ``None`` when no legal counter exists (caller must NO_DEAL).
    """
    if not feasible_bps:
        raise ValueError("next_counter requires at least one feasible bp")

    legal = [bp for bp in feasible_bps if bp <= max_bp and bp < ask_bp]
    if not legal:
        return None

    target = min(ask_bp, max_bp)
    ratio_cap = int(Decimal(str(anchor_ratio)) * Decimal(target))
    under_ratio = [bp for bp in legal if bp <= ratio_cap]
    anchor = max(under_ratio) if under_ratio else min(legal)

    if c_prev is None:
        c_next = anchor
    else:
        delta = target - c_prev
        step = ceil(delta * concession_factor) if delta > 0 else 0
        raw = c_prev + step
        under = [bp for bp in legal if bp <= raw]
        c_next = max(under) if under else c_prev
        if c_next < c_prev:
            c_next = c_prev

    # Never at or above the rep's ask; never above max_bp.
    if c_next >= ask_bp or c_next > max_bp:
        c_next = max(legal)
        if c_prev is not None and c_next < c_prev:
            # Cannot advance legally — stall signal for caller.
            return c_prev if c_prev in legal else None

    if c_next > max_bp or c_next >= ask_bp:
        return None
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


def _confirm_key(belief: BeliefState, ask_bp: int) -> tuple[Any, ...]:
    """Fingerprint of ask + rule values that shape a CONFIRM schedule.

    Includes every field that feeds ``CreditorRules`` so an ASSUMED-field
    change after reject→ASK invalidates the prior key and forces a fresh
    CONFIRM. Values are JSON-safe (dates → ISO) for the audit log.
    """

    def _norm(v: Any) -> Any:
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, list):
            return [(_norm(x) if not isinstance(x, tuple) else list(x)) for x in v]
        if isinstance(v, tuple):
            return [_norm(x) for x in v]
        return v

    return (
        ask_bp,
        _norm(belief.get("max_payments").value),
        _norm(belief.get("min_payment_cents").value),
        _norm(belief.get("payment_structure").value),
        _norm(belief.get("first_payment_date").value),
        _norm(belief.get("max_segments").value),
        _norm(belief.get("max_token_pays").value),
        _norm(belief.get("min_payment_tiers").value),
    )


def _first_assumed_field(belief: BeliefState, *, already_asked: set[str]) -> str | None:
    """Next ASSUMED field to verify after a rejected schedule (registry order)."""
    for spec in FIELD_REGISTRY:
        if spec.name in already_asked:
            continue
        if belief.get(spec.name).status == TermStatus.ASSUMED:
            return spec.name
    return None


def _confirm_action(
    *,
    ask_bp: int,
    belief: BeliefState,
    confirm_facts: dict[str, Fact] | None,
    effects: list[Effect],
    required: set[str],
) -> Action:
    """Emit CONFIRM_SCHEDULE and record its fingerprint on ack."""
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
    key = _confirm_key(belief, ask_bp)
    return Action(
        intent=Intent.CONFIRM_SCHEDULE,
        facts=facts,
        required=required,
        effects=effects
        + [
            Effect(
                kind="record_confirm",
                data={
                    "ask_bp": ask_bp,
                    "key": list(key),
                },
            ),
            Effect(kind="set_phase", data={"phase": Phase.CONFIRM.value}),
        ],
        next_phase=Phase.CONFIRM,
        reason=f"bp={ask_bp}",
    )


def speak_schedule_action(
    rows: list[ScheduleRow],
    *,
    effects: list[Effect] | None = None,
    next_phase: Phase = Phase.CONFIRM,
    reason: str | None = "schedule_detail",
) -> Action:
    """Build a SPEAK_SCHEDULE action with one date/amount fact pair per payment.

    Placeholder ids use letter suffixes (``pay_date_a``) so ``template_guard``
    does not see digits in the template string.
    """
    pays = [r for r in rows if r.creditor_payment_cents > 0][:8]
    letters = "abcdefgh"
    facts: dict[str, Fact] = {}
    parts: list[str] = []
    for letter, row in zip(letters, pays, strict=False):
        d_id = f"pay_date_{letter}"
        a_id = f"pay_amt_{letter}"
        facts[d_id] = Fact(
            id=d_id,
            kind="date",
            value=row.date,
            visibility="PUBLIC",
            source="engine",
        )
        facts[a_id] = Fact(
            id=a_id,
            kind="money",
            value=row.creditor_payment_cents,
            visibility="PUBLIC",
            source="engine",
        )
        parts.append(f"On {{{d_id}}} the creditor payment is {{{a_id}}}.")
    if not parts:
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=list(effects or []),
            next_phase=next_phase,
            reason=reason,
        )
    return Action(
        intent=Intent.SPEAK_SCHEDULE,
        facts=facts,
        required=set(facts),
        effects=list(effects or []),
        next_phase=next_phase,
        reason=reason,
        template_override=" ".join(parts),
    )


def _stall_after_confirm(
    *,
    belief: BeliefState,
    neg: NegotiationState,
    effects: list[Effect],
    max_counters: int,
) -> Action:
    """Rejected/identical CONFIRM: verify each ASSUMED field once, else no-deal."""
    del max_counters  # reserved; once ASSUMED fields exhausted we always end
    effects = list(effects) + [Effect(kind="inc_confirm_reject")]
    fname = _first_assumed_field(belief, already_asked=neg.assumed_asked)
    if fname is not None:
        spec = FIELDS_BY_NAME[fname]
        return Action(
            intent=Intent.ASK,
            text_slots={"ask_text": spec.ask_text, "field_label": spec.label},
            required=set(),
            effects=effects
            + [Effect(kind="note_assumed_asked", data={"field": fname})],
            next_phase=Phase.DISCOVERY,
            reason=fname,
        )
    return Action(
        intent=Intent.NO_DEAL_WRAP,
        text_slots={
            "no_deal_reason": (
                "We could not confirm a schedule both sides can accept."
            )
        },
        effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
        next_phase=Phase.END,
        reason="confirm_rejected",
    )


def _no_deal(
    effects: list[Effect],
    *,
    reason: str,
    spoken_reason: str,
) -> Action:
    """End the call with a speakable reason (no digits)."""
    return Action(
        intent=Intent.NO_DEAL_WRAP,
        text_slots={"no_deal_reason": spoken_reason},
        effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
        next_phase=Phase.END,
        reason=reason,
    )


def _close_after_wrap(effects: list[Effect], *, reason: str) -> Action:
    """Polite thanks after a proposal was already sent to the client."""
    return Action(
        intent=Intent.CLOSE,
        effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
        next_phase=Phase.END,
        reason=reason,
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
    alt_first_payment_date: date | None = None,
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
        return _no_deal(
            effects,
            reason="max_turns",
            spoken_reason="We have reached the limit for this call.",
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

    # Already wrapped: do not re-offer schedules. Thank the rep and end.
    if neg.phase == Phase.WRAP:
        return _close_after_wrap(effects, reason="post_wrap")

    # Already ended / escalated: stay terminal (idle thanks if they keep typing).
    if neg.phase in (Phase.END, Phase.ESCALATE):
        if neg.phase == Phase.ESCALATE:
            return Action(
                intent=Intent.ESCALATE,
                text_slots={
                    "escalate_reason": "A specialist still needs to join this call."
                },
                effects=effects,
                next_phase=Phase.ESCALATE,
                reason="already_escalated",
            )
        return _close_after_wrap(effects, reason="already_ended")

    # Rep wants the payment-by-payment schedule (after we already proposed one).
    # Skip when they are accepting — "payment schedule" often appears in accept lines.
    if (
        analysis.asks_for_schedule
        and neg.last_confirm_key is not None
        and analysis.stance != "accept"
    ):
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=effects,
            next_phase=Phase.CONFIRM,
            reason="schedule_detail",
        )

    # Rep wants to end.
    if analysis.wants_to_end and analysis.stance != "accept":
        # Soft-accept a schedule already on the table, then close with thanks.
        if neg.phase == Phase.CONFIRM and neg.last_confirm_key is not None:
            facts = dict(confirm_facts or {})
            return Action(
                intent=Intent.CLOSE,
                facts=facts,
                effects=effects
                + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
                next_phase=Phase.END,
                reason="thanks_accept",
            )
        return _no_deal(
            effects,
            reason="wants_to_end",
            spoken_reason="Understood — we will end the call here.",
        )

    # Rejected a pending non-price alternative → no deal.
    if (
        neg.pending_terms_alt is not None
        and analysis.stance == "reject"
        and analysis.settlement_ask_pct is None
    ):
        return _no_deal(
            list(effects)
            + [Effect(kind="clear_pending_terms_alt", data={})],
            reason="terms_alt_rejected",
            spoken_reason=(
                "Without an earlier start date we cannot fit a schedule "
                "within the client's program."
            ),
        )

    # 5. Contradictions before read-backs / wrap / asks
    contradicted = belief.contradicted_fields()
    if contradicted:
        fname = contradicted[0]
        if neg.clarify_counts.get(fname, 0) >= 2:
            return Action(
                intent=Intent.ESCALATE,
                text_slots={
                    "escalate_reason": (
                        "I need a specialist to resolve conflicting terms."
                    )
                },
                effects=effects
                + [Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value})],
                next_phase=Phase.ESCALATE,
                reason="contradiction_unresolved",
            )
        term = belief.get(fname)
        old_val = term.history[-1] if term.history else None
        new_val = term.value
        facts: dict[str, Fact] = {}
        text_slots: dict[str, str] = {"field_label": FIELDS_BY_NAME[fname].label}
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
            effects=effects
            + [Effect(kind="note_clarify", data={"field": fname})],
            next_phase=Phase.DISCOVERY,
            reason=fname,
        )

    # 6. Tentative → read-back (before schedule wrap)
    tentative = belief.tentative_fields()
    if tentative:
        fname = tentative[0]
        term = belief.get(fname)
        facts = {}
        text_slots = {"field_label": FIELDS_BY_NAME[fname].label}
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

    # 10: In CONFIRM, accept stance only → wrap (not readback_response).
    if neg.phase == Phase.CONFIRM and analysis.stance == "accept":
        facts = dict(confirm_facts or {})
        return Action(
            intent=Intent.PROPOSE_WRAP,
            facts=facts,
            effects=effects + [Effect(kind="set_phase", data={"phase": Phase.WRAP.value})],
            next_phase=Phase.WRAP,
            reason="confirmed",
        )

    # 7. Missing required field
    missing = belief.missing_required()
    if missing:
        fname = missing[0]
        spec = FIELDS_BY_NAME[fname]
        return Action(
            intent=Intent.ASK,
            text_slots={
                "ask_text": spec.ask_text,
                "field_label": spec.label,
            },
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
        # Nothing feasible at the current first_payment_date.
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
        # Non-price recovery: propose an earlier start date once.
        if alt_first_payment_date is not None:
            alt_iso = alt_first_payment_date.isoformat()
            if alt_iso not in neg.terms_countered:
                return Action(
                    intent=Intent.COUNTER_TERMS,
                    facts={
                        "alt_first_payment_date": Fact(
                            id="alt_first_payment_date",
                            kind="date",
                            value=alt_first_payment_date,
                            visibility="PUBLIC",
                            source="engine",
                        )
                    },
                    required={"alt_first_payment_date"},
                    effects=effects
                    + [
                        Effect(
                            kind="note_terms_countered",
                            data={"field": "first_payment_date", "value": alt_iso},
                        )
                    ],
                    next_phase=Phase.NEGOTIATE,
                    reason="alt_first_payment_date",
                )
        return _no_deal(
            effects,
            reason="infeasible",
            spoken_reason=(
                "No payment schedule fits within the client's program "
                "under these terms."
            ),
        )

    # Rep accepted our last counter → confirm that bp.
    if analysis.stance == "accept" and neg.counters_offered:
        bp = neg.counters_offered[-1]
        return _confirm_action(
            ask_bp=bp,
            belief=belief,
            confirm_facts=confirm_facts,
            effects=effects,
            required=set((confirm_facts or {}).keys())
            & {"offer_total", "num_payments", "first_payment_date"},
        )

    # Ask is affordable and on the feasible grid → confirm schedule at ask.
    if ask_bp <= afford.max_bp and ask_bp in afford.feasible_bps:
        key = _confirm_key(belief, ask_bp)
        if neg.last_confirm_key == key:
            if analysis.stance == "reject":
                return _stall_after_confirm(
                    belief=belief,
                    neg=neg,
                    effects=effects,
                    max_counters=cfg.max_counters,
                )
            # Same schedule already offered; NLU may have missed accept — soft retry.
            soft = list(effects) + [Effect(kind="inc_confirm_reject")]
            if neg.confirm_rejects + 1 >= cfg.max_counters:
                return _no_deal(
                    soft,
                    reason="confirm_unacked",
                    spoken_reason="We still have not confirmed a schedule.",
                )
            return _confirm_action(
                ask_bp=ask_bp,
                belief=belief,
                confirm_facts=confirm_facts,
                effects=soft,
                required=set(),
            )
        return _confirm_action(
            ask_bp=ask_bp,
            belief=belief,
            confirm_facts=confirm_facts,
            effects=effects,
            required=set(),
        )

    # Counter ladder. Stop after MAX_COUNTERS rejections at the highest
    # legal counter (feasible, <= max_bp, and strictly below the ask).
    legal_counters = [
        bp
        for bp in afford.feasible_bps
        if bp <= afford.max_bp and bp < ask_bp
    ]
    ceiling = max(legal_counters) if legal_counters else None
    at_ceiling = (
        bool(neg.counters_offered)
        and ceiling is not None
        and neg.counters_offered[-1] >= ceiling
    )
    if at_ceiling and neg.rejects >= cfg.max_counters:
        return _no_deal(
            effects,
            reason="max_counters",
            spoken_reason="We have exhausted the settlement options we can propose.",
        )

    c_prev = neg.counters_offered[-1] if neg.counters_offered else None
    # If rep rejected and we were at the ceiling, count toward max_counters (on ack).
    if analysis.stance == "reject" and at_ceiling:
        effects.append(Effect(kind="inc_reject_at_max"))

    c_next = next_counter(
        ask_bp=ask_bp,
        max_bp=afford.max_bp,
        feasible_bps=afford.feasible_bps,
        c_prev=c_prev,
        anchor_ratio=cfg.anchor_ratio,
        concession_factor=cfg.concession_factor,
    )
    if c_next is None:
        return _no_deal(
            effects,
            reason="no_legal_counter",
            spoken_reason="We cannot propose a settlement under these terms.",
        )

    # If the ladder stalls below the ceiling, jump to the ceiling once.
    if c_prev is not None and c_next <= c_prev and ceiling is not None and ceiling > c_prev:
        c_next = ceiling

    # Identical / illegal stall: count toward cap then NO_DEAL or COUNTER.
    if c_prev is not None and c_next <= c_prev:
        if not any(e.kind == "inc_reject_at_max" for e in effects):
            effects.append(Effect(kind="inc_reject_at_max"))
        reject_n = neg.rejects + 1
        if reject_n >= cfg.max_counters:
            return _no_deal(
                effects,
                reason="max_counters",
                spoken_reason="We have exhausted the settlement options we can propose.",
            )

    if c_next >= ask_bp or c_next > afford.max_bp:
        return _no_deal(
            effects,
            reason="no_counter_below_ask",
            spoken_reason="We cannot propose a settlement under these terms.",
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
