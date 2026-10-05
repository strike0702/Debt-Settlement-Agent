"""Pure synchronous negotiation policy (PLAN §6.2).

``decide`` maps belief + TurnAnalysis + Affordability to one ``Action``. No I/O,
no LLM; spoken figures ride in PUBLIC ``Action.facts`` and side effects in
``Action.effects`` (the orchestrator applies them on speech ack). The cascade
is first-match and its order is load-bearing:

1. ``_decide_interruptions``: turn cap, hostility, private ask, commitment
   demand, post-proposal phases (``_decide_wrap``), schedule request, end.
2. ``_decide_clarify``: CONTRADICTED → CLARIFY (escalate after two); TENTATIVE → READ_BACK.
3. ``_decide_confirm``: accept of the unchanged CONFIRM on the table → PROPOSE_WRAP.
4. ``_decide_discovery``: ASK a missing required field, then ASK_SETTLEMENT.
5. ``_decide_negotiate``: empty curve (term alt / rescue / no-deal), term alt over
   an unreachable ask, accepted counter, affordable ladder, ceiling ladder
   (≤ ``max_counters`` COUNTERs, never the same bp twice in a row).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    "field_already_countered",
    "next_counter",
    "opening_action",
    "parse_pending_terms_value",
    "terms_counter_key",
]


@dataclass
class NegotiationState:
    """Mutable bargain bookkeeping for one call (ask ladder, counters, flags)."""

    ask_bp: int | None = None
    ask_history: list[int] = field(default_factory=list)
    counters_offered: list[int] = field(default_factory=list)
    private_ask_count: int = 0
    commit_demand_count: int = 0
    pending_readback: str | None = None
    phase: Phase = Phase.OPENING
    turn_idx: int = 0
    # Last CONFIRM fingerprint (ask + rules that shape the schedule).
    last_confirm_key: tuple[Any, ...] | None = None
    # Bp we last proposed in CONFIRM_SCHEDULE (may differ from ask_bp after counters).
    confirmed_bp: int | None = None
    confirm_rejects: int = 0
    assumed_asked: set[str] = field(default_factory=set)
    clarify_counts: dict[str, int] = field(default_factory=dict)
    # Non-price alternatives already offered (``field:value`` keys).
    terms_countered: list[str] = field(default_factory=list)
    # Last COUNTER_TERMS awaiting accept/reject: ``{"field", "value"}``
    # (date values stored as ISO strings).
    pending_terms_alt: dict[str, Any] | None = None
    # Bare money reply awaiting dollars-vs-cents disambiguation.
    # ``{"field", "bare", "as_dollars", "as_cents"}`` — amounts in integer cents.
    pending_cents_clarify: dict[str, Any] | None = None


def terms_counter_key(field: str, value: Any) -> str:
    """Stable key for ``terms_countered`` (dates as ISO)."""
    if isinstance(value, date):
        return f"{field}:{value.isoformat()}"
    return f"{field}:{value}"


def field_already_countered(terms_countered: list[str], field: str) -> bool:
    """True when any prior COUNTER_TERMS was for ``field``.

    Used for ``first_payment_date`` (one try). Min / max may progress to a
    better value when a prior alt left the ask unreachable.
    """
    prefix = f"{field}:"
    return any(k.startswith(prefix) for k in terms_countered)


def parse_pending_terms_value(pending: dict[str, Any]) -> Any:
    """Deserialize pending alt value (ISO date → ``date``)."""
    field = str(pending["field"])
    raw = pending["value"]
    if field == "first_payment_date":
        return date.fromisoformat(str(raw))
    if field in ("min_payment_cents", "max_payments"):
        return int(raw)
    return raw


def _counter_terms_action(
    *,
    field: str,
    value: Any,
    effects: list[Effect],
) -> Action:
    """Build a field-aware COUNTER_TERMS move with the right Fact + template."""
    key = terms_counter_key(field, value)
    if field == "first_payment_date":
        assert isinstance(value, date)
        fact_id = "alt_first_payment_date"
        fact = Fact(
            id=fact_id,
            kind="date",
            value=value,
            visibility="PUBLIC",
            source="engine",
        )
        template = (
            "That start date does not fit the client's program. "
            "Could payment start on {alt_first_payment_date} instead?"
        )
        pending_value: Any = value.isoformat()
    elif field == "min_payment_cents":
        assert isinstance(value, int)
        fact_id = "alt_min_payment_cents"
        fact = Fact(
            id=fact_id,
            kind="money",
            value=value,
            visibility="PUBLIC",
            source="engine",
        )
        template = (
            "These terms do not fit the client's program at that minimum. "
            "Could you allow a lower minimum of {alt_min_payment_cents}?"
        )
        pending_value = value
    elif field == "max_payments":
        assert isinstance(value, int)
        fact_id = "alt_max_payments"
        fact = Fact(
            id=fact_id,
            kind="count",
            value=value,
            visibility="PUBLIC",
            source="engine",
        )
        template = (
            "These terms do not fit the client's program at that payment count. "
            "Could you allow up to {alt_max_payments} payments?"
        )
        pending_value = value
    else:
        raise ValueError(f"unsupported term alt field: {field!r}")

    return Action(
        intent=Intent.COUNTER_TERMS,
        facts={fact_id: fact},
        required={fact_id},
        effects=effects
        + [
            Effect(
                kind="note_terms_countered",
                data={"field": field, "value": pending_value, "key": key},
            )
        ],
        next_phase=Phase.NEGOTIATE,
        reason=fact_id,
        template_override=template,
    )


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


_NUMERIC_KINDS = ("cents", "int", "date")
_FACT_KIND = {"cents": "money", "int": "count", "date": "date"}


def _fact_for_value(fact_id: str, field: str, value: Any) -> Fact:
    """PUBLIC creditor-sourced fact for a cents / int / date field value."""
    kind = _FACT_KIND[FIELDS_BY_NAME[field].kind]
    return Fact(
        id=fact_id,
        kind=kind,  # type: ignore[arg-type]
        value=value if kind == "date" else int(value),
        visibility="PUBLIC",
        source="creditor",
    )


def _spoken_value(
    fact_id: str,
    field: str,
    value: Any,
    facts: dict[str, Fact],
    text_slots: dict[str, str],
    required: set[str],
) -> None:
    """Numeric values speak via a PUBLIC fact placeholder; enums and tiers via a text slot."""
    if value is not None and FIELDS_BY_NAME[field].kind in _NUMERIC_KINDS:
        facts[fact_id] = _fact_for_value(fact_id, field, value)
        required.add(fact_id)
    else:
        text_slots[fact_id] = str(value)


def cents_ambiguity_clarify_action(
    *,
    field: str,
    bare: int,
    effects: list[Effect] | None = None,
) -> Action:
    """Ask whether a bare number is dollars or cents before storing the term."""
    as_dollars = bare * 100
    as_cents = bare
    label = FIELDS_BY_NAME[field].label
    pending = {
        "field": field,
        "bare": bare,
        "as_dollars": as_dollars,
        "as_cents": as_cents,
    }
    return Action(
        intent=Intent.CLARIFY,
        facts={
            "bare_amount": Fact(
                id="bare_amount",
                kind="count",
                value=bare,
                visibility="PUBLIC",
                source="creditor",
            ),
            "clarify_old": Fact(
                id="clarify_old",
                kind="money",
                value=as_dollars,
                visibility="PUBLIC",
                source="creditor",
            ),
            "clarify_new": Fact(
                id="clarify_new",
                kind="money",
                value=as_cents,
                visibility="PUBLIC",
                source="creditor",
            ),
        },
        text_slots={"field_label": label},
        required={"bare_amount", "clarify_old", "clarify_new"},
        effects=list(effects or [])
        + [
            Effect(kind="set_pending_cents_clarify", data=pending),
            Effect(kind="note_clarify", data={"field": field}),
        ],
        next_phase=Phase.DISCOVERY,
        reason="cents_ambiguity",
        template_override=(
            "I heard {bare_amount} for the {field_label}. "
            "Is that {clarify_old}, or {clarify_new}?"
        ),
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
    reason: str | None = None,
) -> Action:
    """Emit CONFIRM_SCHEDULE and record its fingerprint on ack.

    Always overwrites ``settlement_pct`` with ``ask_bp`` so stale confirm_facts
    cannot speak a different percentage than the agreement.
    """
    facts = dict(confirm_facts or {})
    facts["settlement_pct"] = Fact(
        id="settlement_pct",
        kind="pct",
        value=ask_bp,
        visibility="PUBLIC",
        source="engine",
    )
    key = _confirm_key(belief, ask_bp)
    return Action(
        intent=Intent.CONFIRM_SCHEDULE,
        facts=facts,
        required=set(required) | {"settlement_pct"},
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
        reason=reason if reason is not None else f"bp={ask_bp}",
    )


def _counter_action(
    c_next: int,
    effects: list[Effect],
    *,
    counter_offer_total_cents: int | None = None,
) -> Action:
    """Emit COUNTER at ``c_next`` with optional spoken offer total."""
    facts: dict[str, Fact] = {
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


def _confirm_required(confirm_facts: dict[str, Fact] | None) -> set[str]:
    """Schedule facts a CONFIRM must speak when the orchestrator supplied them."""
    return set(confirm_facts or {}) & {"offer_total", "num_payments", "first_payment_date"}


def _negotiate_affordable(t: _Turn, ask_bp: int, afford: Affordability) -> Action:
    """Counter ladder when the ask is affordable; never NO_DEAL from this path.

    Order: ask within prior offer → firm → counters exhausted → no lower
    counter → ladder stall jump → close-gap confirm → COUNTER.
    """
    neg, cfg = t.neg, t.cfg
    assert afford.max_bp is not None

    def confirm(reason: str) -> Action:
        return _confirm_action(
            ask_bp=ask_bp,
            belief=t.belief,
            confirm_facts=t.confirm_facts,
            effects=t.effects,
            required=_confirm_required(t.confirm_facts),
            reason=reason,
        )

    prior = list(neg.counters_offered)
    if neg.confirmed_bp is not None:
        prior.append(neg.confirmed_bp)
    if prior and ask_bp <= max(prior):
        return confirm("ask_within_offer")
    if t.analysis.firm and neg.counters_offered:
        return confirm("rep_firm")
    if len(neg.counters_offered) >= cfg.max_counters:
        return confirm("counters_exhausted")

    c_prev = neg.counters_offered[-1] if neg.counters_offered else None
    c_next = next_counter(
        ask_bp=ask_bp,
        max_bp=afford.max_bp,
        feasible_bps=afford.feasible_bps,
        c_prev=c_prev,
        anchor_ratio=cfg.anchor_ratio,
        concession_factor=cfg.concession_factor,
    )
    if c_next is None:
        return confirm("no_lower_counter")
    # Ladder stalled on a feasibility gap: jump to the best legal bp, else confirm.
    if c_prev is not None and c_next <= c_prev:
        jump = [
            bp
            for bp in afford.feasible_bps
            if c_prev < bp <= afford.max_bp and bp < ask_bp
        ]
        if not jump:
            return confirm("ladder_stalled")
        c_next = max(jump)
    if ask_bp - c_next <= cfg.close_gap_bp:
        return confirm("gap_small")
    return _counter_action(
        c_next, t.effects, counter_offer_total_cents=t.counter_offer_total_cents
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


def _ask_field(fname: str, effects: list[Effect]) -> Action:
    """ASK for one registry field using its rep-facing copy."""
    spec = FIELDS_BY_NAME[fname]
    return Action(
        intent=Intent.ASK,
        text_slots={"ask_text": spec.ask_text, "field_label": spec.label},
        required=set(),
        effects=effects,
        next_phase=Phase.DISCOVERY,
        reason=fname,
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


def _escalate(effects: list[Effect], *, reason: str, spoken_reason: str) -> Action:
    """Hand off to a human and move to ESCALATE (no digits in the reason)."""
    return Action(
        intent=Intent.ESCALATE,
        text_slots={"escalate_reason": spoken_reason},
        effects=effects + [Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value})],
        next_phase=Phase.ESCALATE,
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


def _wrap_should_renegotiate(analysis: TurnAnalysis) -> bool:
    """True when the rep reopens price or terms after PROPOSE_WRAP."""
    if analysis.settlement_ask_pct is not None:
        return True
    if analysis.stance in ("reject", "counter", "offer"):
        return True
    if analysis.terms:
        return True
    return False


@dataclass(frozen=True)
class _Turn:
    """Inputs of one ``decide`` call after the prelude (ask recorded, rejected alt cleared)."""

    belief: BeliefState
    neg: NegotiationState
    analysis: TurnAnalysis
    afford: Affordability | None
    cfg: Settings
    ask_bp: int | None
    effects: list[Effect]
    rescue_within_guardrail: bool
    confirm_facts: dict[str, Fact] | None
    counter_offer_total_cents: int | None
    term_alt: tuple[str, Any] | None
    accepted_term_alt: bool


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
    term_alt: tuple[str, Any] | None = None,
    accepted_term_alt: bool = False,
) -> Action:
    """Return the next agent ``Action``: first match of the module-docstring cascade.

    ``term_alt`` is ``(field, value)`` for the next non-price recovery stage
    when the curve is empty or the ask sits above the ceiling
    (first_payment_date → min_payment → max_payments).

    ``accepted_term_alt`` is true when this turn's accept applied a pending
    non-price alternative. That yes is not acceptance of the last price.
    """
    ask_bp = neg.ask_bp
    effects: list[Effect] = []
    if analysis.settlement_ask_pct is not None:
        ask_bp = ask_pct_to_bp(analysis.settlement_ask_pct)
        effects.append(Effect(kind="record_ask", data={"bp": ask_bp}))
    # A rejected pending non-price alt is cleared so the cascade offers the next stage.
    if (
        neg.pending_terms_alt is not None
        and analysis.stance == "reject"
        and analysis.settlement_ask_pct is None
    ):
        effects.append(Effect(kind="clear_pending_terms_alt", data={}))
        neg = replace(neg, pending_terms_alt=None)

    t = _Turn(
        belief=belief,
        neg=neg,
        analysis=analysis,
        afford=afford,
        cfg=settings or get_settings(),
        ask_bp=ask_bp,
        effects=effects,
        rescue_within_guardrail=rescue_within_guardrail,
        confirm_facts=confirm_facts,
        counter_offer_total_cents=counter_offer_total_cents,
        term_alt=term_alt,
        accepted_term_alt=accepted_term_alt,
    )
    for step in (_decide_interruptions, _decide_clarify, _decide_confirm, _decide_discovery):
        action = step(t)
        if action is not None:
            return action
    return _decide_negotiate(t)


def _decide_interruptions(t: _Turn) -> Action | None:
    """Moves that preempt the phase flow, in priority order."""
    a, neg, cfg = t.analysis, t.neg, t.cfg
    if neg.turn_idx > cfg.max_turns:
        return _no_deal(
            t.effects,
            reason="max_turns",
            spoken_reason="We have reached the limit for this call.",
        )
    if a.hostility >= cfg.hostility_threshold:
        return _escalate(
            t.effects,
            reason="hostile",
            spoken_reason="The tone on this call needs a specialist.",
        )
    # Private-info and commitment demands: refuse once, escalate on repeat.
    stay = neg.phase if neg.phase != Phase.OPENING else Phase.DISCOVERY
    if a.asks_client_private_info:
        bumped = t.effects + [Effect(kind="inc_private_ask")]
        if neg.private_ask_count >= 1:
            return _escalate(
                bumped,
                reason="sensitive_request",
                spoken_reason="I need to hand this off after a sensitive request.",
            )
        return Action(
            intent=Intent.REFUSE_PRIVATE,
            effects=bumped,
            next_phase=stay,
            reason="private_info",
        )
    if a.demands_commitment:
        bumped = t.effects + [Effect(kind="inc_commit_demand")]
        if neg.commit_demand_count >= 1:
            return _escalate(
                bumped,
                reason="commitment_demand",
                spoken_reason="I need to involve someone about a commitment demand.",
            )
        return Action(
            intent=Intent.REFUSE_COMMIT,
            effects=bumped,
            next_phase=stay,
            reason="commitment",
        )
    if neg.phase in (Phase.WRAP, Phase.END, Phase.ESCALATE):
        return _decide_wrap(t)
    # Schedule detail after a proposal; "payment schedule" often appears in accept lines.
    if a.asks_for_schedule and neg.last_confirm_key is not None and a.stance != "accept":
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=t.effects,
            next_phase=Phase.CONFIRM,
            reason="schedule_detail",
        )
    if a.wants_to_end and a.stance != "accept":
        # Soft-accept a schedule already on the table, then close with thanks.
        if neg.phase == Phase.CONFIRM and neg.last_confirm_key is not None:
            return Action(
                intent=Intent.CLOSE,
                facts=dict(t.confirm_facts or {}),
                effects=t.effects
                + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
                next_phase=Phase.END,
                reason="thanks_accept",
            )
        return _no_deal(
            t.effects,
            reason="wants_to_end",
            spoken_reason="Understood — we will end the call here.",
        )
    return None


def _decide_wrap(t: _Turn) -> Action:
    """Post-proposal phases: stay terminal, or reopen price/terms from WRAP."""
    a, neg = t.analysis, t.neg
    if neg.phase == Phase.ESCALATE:
        return Action(
            intent=Intent.ESCALATE,
            text_slots={"escalate_reason": "A specialist still needs to join this call."},
            effects=t.effects,
            next_phase=Phase.ESCALATE,
            reason="already_escalated",
        )
    if neg.phase == Phase.END:
        return _close_after_wrap(t.effects, reason="already_ended")
    if a.asks_for_schedule and a.stance != "accept":
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=t.effects,
            next_phase=Phase.WRAP,
            reason="schedule_detail_post_wrap",
        )
    if not _wrap_should_renegotiate(a):
        return _close_after_wrap(t.effects, reason="post_wrap")
    # Re-enter as CONFIRM/NEGOTIATE; the drafted agreement clears on ack.
    reopen = Phase.CONFIRM if neg.confirmed_bp is not None else Phase.NEGOTIATE
    action = decide(
        t.belief,
        replace(neg, phase=reopen),
        a,
        t.afford,
        settings=t.cfg,
        rescue_within_guardrail=t.rescue_within_guardrail,
        confirm_facts=t.confirm_facts,
        counter_offer_total_cents=t.counter_offer_total_cents,
        term_alt=t.term_alt,
        accepted_term_alt=t.accepted_term_alt,
    )
    return action.model_copy(
        update={
            "effects": [Effect(kind="clear_wrap", data={}), *action.effects],
            "reason": action.reason or "wrap_renegotiate",
        }
    )


def _decide_clarify(t: _Turn) -> Action | None:
    """Resolve CONTRADICTED (CLARIFY, escalate after two) then TENTATIVE (READ_BACK)."""
    belief = t.belief
    contradicted = belief.contradicted_fields()
    if contradicted:
        fname = contradicted[0]
        if t.neg.clarify_counts.get(fname, 0) >= 2:
            return _escalate(
                t.effects,
                reason="contradiction_unresolved",
                spoken_reason="I need a specialist to resolve conflicting terms.",
            )
        term = belief.get(fname)
        facts: dict[str, Fact] = {}
        slots = {"field_label": FIELDS_BY_NAME[fname].label}
        required: set[str] = set()
        old = term.history[-1] if term.history else None
        _spoken_value("clarify_old", fname, old, facts, slots, required)
        _spoken_value("clarify_new", fname, term.value, facts, slots, required)
        return Action(
            intent=Intent.CLARIFY,
            facts=facts,
            text_slots=slots,
            required=required,
            effects=t.effects + [Effect(kind="note_clarify", data={"field": fname})],
            next_phase=Phase.DISCOVERY,
            reason=fname,
        )
    tentative = belief.tentative_fields()
    if tentative:
        fname = tentative[0]
        facts = {}
        slots = {"field_label": FIELDS_BY_NAME[fname].label}
        required = set()
        _spoken_value("readback_value", fname, belief.get(fname).value, facts, slots, required)
        return Action(
            intent=Intent.READ_BACK,
            facts=facts,
            text_slots=slots,
            required=required,
            effects=t.effects + [Effect(kind="set_pending_readback", data={"field": fname})],
            next_phase=Phase.DISCOVERY,
            reason=fname,
        )
    return None


def _propose_wrap(t: _Turn) -> Action:
    """Send the confirmed schedule for client approval."""
    return Action(
        intent=Intent.PROPOSE_WRAP,
        facts=dict(t.confirm_facts or {}),
        effects=t.effects + [Effect(kind="set_phase", data={"phase": Phase.WRAP.value})],
        next_phase=Phase.WRAP,
        reason="confirmed",
    )


def _decide_confirm(t: _Turn) -> Action | None:
    """CONFIRM-phase accept of the unchanged schedule on the table → PROPOSE_WRAP.

    Not a wrap: ``readback_response``, a restated different % (a correction),
    a yes to a pending term alt, or a fingerprint that changed since CONFIRM.
    """
    a, neg = t.analysis, t.neg
    if neg.phase != Phase.CONFIRM or a.stance != "accept" or t.accepted_term_alt:
        return None
    if neg.confirmed_bp is None or neg.last_confirm_key is None:
        return None
    if (
        a.settlement_ask_pct is not None
        and ask_pct_to_bp(a.settlement_ask_pct) != neg.confirmed_bp
    ):
        return None
    if _confirm_key(t.belief, neg.confirmed_bp) != neg.last_confirm_key:
        return None
    return _propose_wrap(t)


def _decide_discovery(t: _Turn) -> Action | None:
    """ASK the next missing required field, then the settlement ask."""
    missing = t.belief.missing_required()
    if missing:
        return _ask_field(missing[0], t.effects)
    # afford is None only when rules are not buildable (missing fields caught above).
    if t.ask_bp is None or t.afford is None:
        return Action(
            intent=Intent.ASK_SETTLEMENT,
            effects=t.effects,
            next_phase=Phase.DISCOVERY,
        )
    return None


def _decide_negotiate(t: _Turn) -> Action:
    """Price and non-price moves once rules and the ask are known."""
    ask_bp, afford = t.ask_bp, t.afford
    assert ask_bp is not None and afford is not None
    if afford.max_bp is None:
        # Nothing feasible at this start date: FPD → min → max_pay alts, then rescue.
        alt = _term_alt_action(t)
        if alt is not None:
            return alt
        if t.rescue_within_guardrail:
            return _escalate(
                t.effects,
                reason="out_of_guardrail",
                spoken_reason="This needs client approval for extra funds before we continue.",
            )
        return _no_deal(
            t.effects,
            reason="infeasible",
            spoken_reason=(
                "No payment schedule fits within the client's program under these terms."
            ),
        )
    # A further non-price alt that can raise the ceiling beats a price counter.
    if ask_bp > afford.max_bp:
        alt = _term_alt_action(t)
        if alt is not None:
            return alt
    accepted = _confirm_accepted_counter(t, ask_bp, afford)
    if accepted is not None:
        return accepted
    if ask_bp <= afford.max_bp and ask_bp in afford.feasible_bps:
        on_table = _reconfirm_on_table(t, ask_bp, afford)
        if on_table is not None:
            return on_table
        return _negotiate_affordable(t, ask_bp, afford)
    return _ladder_unreachable(t, ask_bp, afford)


def _term_alt_action(t: _Turn) -> Action | None:
    """COUNTER_TERMS for ``t.term_alt`` unless already offered (FPD gets one try)."""
    if t.term_alt is None:
        return None
    alt_field, alt_value = t.term_alt
    if terms_counter_key(alt_field, alt_value) in t.neg.terms_countered:
        return None
    if alt_field == "first_payment_date" and field_already_countered(
        t.neg.terms_countered, alt_field
    ):
        return None
    return _counter_terms_action(field=alt_field, value=alt_value, effects=t.effects)


def _confirm_accepted_counter(t: _Turn, ask_bp: int, afford: Affordability) -> Action | None:
    """Rep accepted: confirm a restated % or our last counter, if still feasible.

    A yes to a term alt is not a yes to the previous price — that counter can
    be a token percent the new terms just made obsolete.
    """
    a, neg = t.analysis, t.neg
    if a.stance != "accept":
        return None
    assert afford.max_bp is not None
    legal = {bp for bp in afford.feasible_bps if bp <= afford.max_bp}
    bp: int | None = None
    if a.settlement_ask_pct is not None:
        stated = ask_pct_to_bp(a.settlement_ask_pct)
        if stated in legal:
            bp = stated
    if bp is None and neg.counters_offered and not t.accepted_term_alt:
        if neg.counters_offered[-1] in legal:
            bp = neg.counters_offered[-1]
    if bp is None or (t.accepted_term_alt and bp < ask_bp):
        return None
    return _confirm_action(
        ask_bp=bp,
        belief=t.belief,
        confirm_facts=t.confirm_facts,
        effects=t.effects,
        required=_confirm_required(t.confirm_facts),
    )


def _reconfirm_on_table(t: _Turn, ask_bp: int, afford: Affordability) -> Action | None:
    """Affordable ask with a CONFIRM already spoken: re-confirm, stall, or wrap.

    Terms revised since CONFIRM → re-confirm at ``confirmed_bp``. Same schedule
    and it meets the ask → reject probes ASSUMED fields, accept wraps (phase-lag
    safety net), anything else soft-retries up to ``max_counters``. A confirm
    still below the ask, or a new lower ask, falls through to the ladder.
    """
    neg, a = t.neg, t.analysis
    confirmed = neg.confirmed_bp
    if confirmed is None or neg.last_confirm_key is None:
        return None
    assert afford.max_bp is not None
    new_ask = a.settlement_ask_pct is not None
    key_matches = _confirm_key(t.belief, confirmed) == neg.last_confirm_key
    if (
        not new_ask
        and not key_matches
        and confirmed <= afford.max_bp
        and confirmed in afford.feasible_bps
        and ask_bp <= confirmed
    ):
        return _confirm_action(
            ask_bp=confirmed,
            belief=t.belief,
            confirm_facts=t.confirm_facts,
            effects=t.effects,
            required=_confirm_required(t.confirm_facts),
            reason="terms_revised",
        )
    if ask_bp > confirmed or not key_matches or (new_ask and ask_bp < confirmed):
        return None
    if a.stance == "reject":
        return _stall_after_confirm(t)
    if a.stance == "accept":
        return _propose_wrap(t)
    soft = t.effects + [Effect(kind="inc_confirm_reject")]
    if neg.confirm_rejects + 1 >= t.cfg.max_counters:
        return _no_deal(
            soft,
            reason="confirm_unacked",
            spoken_reason="We still have not confirmed a schedule.",
        )
    return _confirm_action(
        ask_bp=confirmed,
        belief=t.belief,
        confirm_facts=t.confirm_facts,
        effects=soft,
        required=set(),
    )


def _stall_after_confirm(t: _Turn) -> Action:
    """Rejected identical CONFIRM: verify each ASSUMED field once, else no-deal."""
    effects = t.effects + [Effect(kind="inc_confirm_reject")]
    fname = _first_assumed_field(t.belief, already_asked=t.neg.assumed_asked)
    if fname is not None:
        return _ask_field(
            fname, effects + [Effect(kind="note_assumed_asked", data={"field": fname})]
        )
    return _no_deal(
        effects,
        reason="confirm_rejected",
        spoken_reason="We could not confirm a schedule both sides can accept.",
    )


def _ladder_unreachable(t: _Turn, ask_bp: int, afford: Affordability) -> Action:
    """Counter ladder when the ask is above the ceiling (or off the grid).

    Ceiling = highest feasible bp <= max_bp and strictly below the ask. At most
    ``max_counters`` COUNTERs; the last one is the ceiling. Once the ceiling is
    on the table there is nothing better to say, so any non-accept turn ends
    (re-offering the same bp was the Phase 12 ten-counter loop).
    """
    neg, cfg = t.neg, t.cfg
    assert afford.max_bp is not None
    c_prev = neg.counters_offered[-1] if neg.counters_offered else None
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
            t.effects,
            reason="no_legal_counter",
            spoken_reason="We cannot propose a settlement under these terms.",
        )
    ceiling = max(
        bp for bp in afford.feasible_bps if bp <= afford.max_bp and bp < ask_bp
    )
    if len(neg.counters_offered) >= cfg.max_counters or (
        c_prev is not None and c_prev >= ceiling
    ):
        return _no_deal(
            t.effects,
            reason="max_counters",
            spoken_reason="We have exhausted the settlement options we can propose.",
        )
    if len(neg.counters_offered) + 1 >= cfg.max_counters or (
        c_prev is not None and c_next <= c_prev
    ):
        c_next = ceiling
    return _counter_action(
        c_next, t.effects, counter_offer_total_cents=t.counter_offer_total_cents
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
