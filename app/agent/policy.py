"""Pure synchronous negotiation policy (PLAN §6.2).

``decide`` maps belief + TurnAnalysis + Affordability to one ``Action``. No I/O,
no LLM; spoken figures ride in PUBLIC ``Action.facts`` and side effects in
``Action.effects`` (the orchestrator applies them on speech ack). The cascade
is first-match and its order is load-bearing:

1. ``_decide_interruptions``: turn cap, hostility, private ask, commitment
   demand, post-proposal phases (``_decide_wrap``), no-progress loop guard,
   schedule request, end.
2. ``_decide_clarify``: "first N payments" tiers / CONTRADICTED → CLARIFY (escalate
   after two); TENTATIVE → READ_BACK.
3. ``_decide_confirm``: accept of the unchanged CONFIRM on the table → PROPOSE_WRAP.
4. ``_decide_discovery``: ASK a missing required field, then ASK_SETTLEMENT.
5. ``_decide_negotiate``: empty curve (term alt / rescue / handoff), term alt over
   an unreachable ask, accepted counter, the price ladder (``_negotiate``).

After the cascade, ``loop_guard`` turns a question asked twice already into a
handoff (``repeated_question``).

Call endings (Phase 45): a call ends only as a confirmed deal (PROPOSE_WRAP /
``thanks_accept``) or a handoff (ESCALATE, with a reason code for the person
taking over). ``decide`` never emits NO_DEAL_WRAP.

Price ladder (Phase 45, ``_negotiate``). The *accept line* is 75% of the
client's ceiling (``accept_line_bp``, rounded down); we never offer above it
and accept only at or below it, and only at a bp the engine can schedule.

- The rep's first number is never accepted: we counter at the anchor
  (``anchor_ratio`` × min(ask, line)). Only when no lower legal counter exists
  may we accept it (``no_lower_counter``).
- Rep moved down since our last counter: we concede half their move
  (``concession_factor``), and the hold / step count resets.
- Rep did not move: hold once (restate our offer), then two small steps of a
  quarter of the gap to min(ask, line); then accept if their ask is at or below
  the line (``rep_held``), else hand off (``above_accept_line``).
- Rep firm: above the line → hand off now; else one final counter halfway
  between our last offer and their number, and accept when they repeat it
  (``rep_firm``).
- ``max_counters`` spoken counters (holds included) caps the ladder: accept at
  or below the line (``counters_exhausted``), else hand off (``max_counters``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from string import ascii_lowercase
from typing import Any, Literal

from pydantic import BaseModel

from app.adapter.engine_adapter import Affordability
from app.config import Settings, get_settings
from app.domain.actions import Action, Effect, Intent, Phase
from app.domain.belief import BeliefChange, BeliefState, TermStatus
from app.domain.facts import Fact
from app.domain.fields import FIELD_REGISTRY, FIELDS_BY_NAME
from app.domain.negotiation import accept_line_bp
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
    "amount_meaning_clarify_action",
    "amount_meaning_escalate_action",
    "anchor_bp",
    "ask_pct_to_bp",
    "decide",
    "draft_agreement",
    "field_already_countered",
    "loop_guard",
    "opening_action",
    "parse_pending_terms_value",
    "question_key",
    "rep_turn_progress",
    "step_bp",
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
    # Confirm key the rep accepted while a READ_BACK preempted the wrap.
    accepted_confirm_key: tuple[Any, ...] | None = None
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
    # Dollar amount awaiting "total or per payment?" (Phase 39). Set and cleared
    # by the orchestrator: ``{"cents", "quote", "trigger"}`` plus ``"pct"`` /
    # ``"pct_quote"`` when a disagreeing % ask was held back.
    pending_amount_clarify: dict[str, Any] | None = None
    # Phase 45 price ladder. Rep's ask when our last counter was spoken (did
    # they move since?), hold / step stage (0 none, 1 held, 2–3 small steps),
    # the rep's firm ask when we made our final counter, and the turns that
    # spoke a COUNTER (holds included) for the ``max_counters`` cap.
    ask_at_last_counter: int | None = None
    hold_stage: int = 0
    final_counter_ask: int | None = None
    counter_turns: list[int] = field(default_factory=list)
    # Phase 45 loop guard: times each question was asked (``question_key``) and
    # rep turns in a row that added nothing (``rep_turn_progress``).
    question_counts: dict[str, int] = field(default_factory=dict)
    no_progress_turns: int = 0


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


def anchor_bp(*, ask_bp: int, line_bp: int, legal: list[int], anchor_ratio: float) -> int | None:
    """Opening counter: highest legal bp ≤ ``anchor_ratio`` × min(ask, line), below the ask.

    ``legal`` is the feasible grid at or below the accept line. Falls back to
    the lowest legal bp below the ask; ``None`` when nothing legal is below it.
    """
    below = [bp for bp in legal if bp < ask_bp]
    if not below:
        return None
    cap = int(Decimal(str(anchor_ratio)) * Decimal(min(ask_bp, line_bp)))
    under = [bp for bp in below if bp <= cap]
    return max(under) if under else min(below)


def step_bp(*, prev: int, raw: int, ceiling: int, legal: list[int]) -> int | None:
    """Next counter above ``prev``: highest legal bp ≤ ``raw``, else the next legal bp up.

    Never above ``ceiling`` (exclusive bound = the rep's ask, or the line + 1).
    ``None`` when no legal bp lies strictly between ``prev`` and ``ceiling``.
    """
    room = [bp for bp in legal if prev < bp < ceiling]
    if not room:
        return None
    under = [bp for bp in room if bp <= raw]
    return max(under) if under else min(room)


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


_NO_TIERS_TEXT = "no special payment tiers"


def _tiers_phrase(
    prefix: str, tiers: Any, facts: dict[str, Fact], required: set[str]
) -> str:
    """Template fragment for ``[(from_payment, min_cents)]``; figures ride in PUBLIC facts."""
    if not tiers:
        return _NO_TIERS_TEXT
    parts: list[str] = []
    # Placeholder ids must be digit-free (template guard), so tiers are lettered.
    for tag, (frm, cents) in zip(ascii_lowercase, tiers, strict=False):
        from_id, min_id = f"{prefix}_tier_{tag}_from", f"{prefix}_tier_{tag}_min"
        facts[from_id] = Fact(
            id=from_id, kind="ordinal", value=int(frm), visibility="PUBLIC", source="creditor"
        )
        facts[min_id] = Fact(
            id=min_id, kind="money", value=int(cents), visibility="PUBLIC", source="creditor"
        )
        required.update((from_id, min_id))
        parts.append(f"a minimum of {{{min_id}}} from the {{{from_id}}} payment on")
    return " and ".join(parts)


def _spoken_value(
    fact_id: str,
    field: str,
    value: Any,
    facts: dict[str, Fact],
    text_slots: dict[str, str],
    required: set[str],
) -> str:
    """Stage ``value`` for speech and return the template fragment that speaks it.

    Numbers ride in PUBLIC facts and enums in a text slot (both ``{fact_id}``).
    Tiers return a whole phrase over per-tier ordinal + money facts, so the
    caller must use it as a ``template_override``.
    """
    kind = FIELDS_BY_NAME[field].kind
    if kind == "tiers":
        return _tiers_phrase(fact_id, value, facts, required)
    if value is not None and kind in _NUMERIC_KINDS:
        facts[fact_id] = _fact_for_value(fact_id, field, value)
        required.add(fact_id)
    else:
        text_slots[fact_id] = str(value)
    return "{" + fact_id + "}"


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


# ``clarify_counts`` key for the total-vs-per-payment question (Phase 39).
AMOUNT_CLARIFY_KEY = "amount_meaning"


def amount_meaning_clarify_action(*, cents: int) -> Action:
    """Ask whether a dollar amount is the total settlement or the per-payment minimum.

    Counts toward ``clarify_counts[AMOUNT_CLARIFY_KEY]``; the orchestrator
    escalates once it reaches two unresolved asks.
    """
    return Action(
        intent=Intent.CLARIFY,
        facts={
            "amount_in_question": Fact(
                id="amount_in_question",
                kind="money",
                value=cents,
                visibility="PUBLIC",
                source="creditor",
            )
        },
        required={"amount_in_question"},
        effects=[Effect(kind="note_clarify", data={"field": AMOUNT_CLARIFY_KEY})],
        next_phase=Phase.DISCOVERY,
        reason="amount_meaning",
        template_override=(
            "Just to be sure: is {amount_in_question} the total settlement, "
            "or the minimum for each payment?"
        ),
    )


def amount_meaning_escalate_action() -> Action:
    """Hand off after two unanswered total-vs-per-payment questions."""
    return _escalate(
        [],
        reason="amount_meaning_unresolved",
        spoken_reason="I need a specialist to confirm what that amount covers.",
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


# COUNTER templates for the Phase 45 ladder moves; ``{offer_total}`` is required
# because ``Orchestrator._enrich_action`` makes it so for every COUNTER.
_HOLD_TEMPLATE = (
    "We are staying at {counter_pct} of the balance, which is {offer_total}. "
    "Can you come down from your number?"
)
_FINAL_COUNTER_TEMPLATE = (
    "We can meet you partway at {counter_pct} of the balance, which is "
    "{offer_total}. Would that work?"
)


def _counter_action(
    c_next: int,
    effects: list[Effect],
    *,
    counter_offer_total_cents: int | None = None,
    ask_bp: int | None = None,
    turn: int | None = None,
    stage: int = 0,
    final_ask: int | None = None,
    reason: str | None = None,
    template: str | None = None,
) -> Action:
    """Emit COUNTER at ``c_next`` with optional spoken offer total.

    ``offer_counter`` carries the ladder bookkeeping (rep's ask now, hold / step
    stage after this move, final-counter ask, turn), applied idempotently on
    eager emit and again on speech ack.
    """
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
    data: dict[str, Any] = {"bp": c_next, "stage": stage}
    if ask_bp is not None:
        data["ask_bp"] = ask_bp
    if turn is not None:
        data["turn"] = turn
    if final_ask is not None:
        data["final_ask"] = final_ask
    return Action(
        intent=Intent.COUNTER,
        facts=facts,
        required={"counter_pct"},
        effects=effects
        + [
            Effect(kind="offer_counter", data=data),
            Effect(kind="set_phase", data={"phase": Phase.NEGOTIATE.value}),
        ],
        next_phase=Phase.NEGOTIATE,
        reason=reason or f"bp={c_next}",
        template_override=template,
    )


def _confirm_required(confirm_facts: dict[str, Fact] | None) -> set[str]:
    """Schedule facts a CONFIRM must speak when the orchestrator supplied them."""
    return set(confirm_facts or {}) & {"offer_total", "num_payments", "first_payment_date"}


# Small steps after a hold are a quarter of the gap to min(ask, line).
_HOLD_STEP_DIVISOR = 4
# Hold stages: 1 = held once, 2 and 3 = first and second small step taken.
_LAST_HOLD_STAGE = 3


def _negotiate(t: _Turn, ask_bp: int, afford: Affordability) -> Action:
    """The Phase 45 price ladder (module docstring); every accept is a CONFIRM at a feasible bp.

    Order: ask within our offer → rep's first number → repeat after our final
    counter → firm → ``max_counters`` cap → rep moved (concede half) → hold →
    two small steps → accept at or below the line, else hand off.
    """
    neg, cfg, a = t.neg, t.cfg, t.analysis
    assert afford.max_bp is not None
    line = accept_line_bp(afford.max_bp, cfg.accept_line_pct_of_max_bp)
    legal = sorted(bp for bp in afford.feasible_bps if bp <= line)
    acceptable = ask_bp <= line and ask_bp in afford.feasible_bps

    def confirm(bp: int, reason: str) -> Action:
        return _confirm_action(
            ask_bp=bp,
            belief=t.belief,
            confirm_facts=t.confirm_facts,
            effects=t.effects,
            required=_confirm_required(t.confirm_facts),
            reason=reason,
        )

    def settle(accept_reason: str, over_line_reason: str = "above_accept_line") -> Action:
        if acceptable:
            return confirm(ask_bp, accept_reason)
        reason = over_line_reason if ask_bp > line else "no_legal_counter"
        return _handoff(t.effects, reason)

    def counter(bp: int, stage: int, **kw: Any) -> Action:
        return _counter_action(
            bp,
            t.effects,
            counter_offer_total_cents=t.counter_offer_total_cents,
            ask_bp=ask_bp,
            turn=neg.turn_idx,
            stage=stage,
            **kw,
        )

    prior = list(neg.counters_offered)
    if neg.confirmed_bp is not None:
        prior.append(neg.confirmed_bp)
    # Rep now asks no more than we already offered: take the lower number.
    if prior and ask_bp <= max(prior):
        if acceptable:
            return confirm(ask_bp, "ask_within_offer")
        best = max(prior)
        if best <= line and best in afford.feasible_bps:
            return confirm(best, "ask_within_offer")

    c_prev = neg.counters_offered[-1] if neg.counters_offered else None
    if c_prev is not None and c_prev > line:
        # Revised terms lowered the line under our last offer: never restate it.
        return settle("rep_held")
    if c_prev is not None and t.accepted_term_alt:
        # A yes to a term change reshaped the curve: re-anchor if that moves us up
        # (the old counter can be a token percent the new terms made obsolete).
        fresh = anchor_bp(ask_bp=ask_bp, line_bp=line, legal=legal, anchor_ratio=cfg.anchor_ratio)
        if fresh is not None and fresh > c_prev:
            return counter(fresh, 0)
    if c_prev is None:
        # The rep's first number is never accepted while a lower legal counter exists.
        if a.firm and ask_bp > line:
            return _handoff(t.effects, "above_accept_line")
        anchor = anchor_bp(
            ask_bp=ask_bp, line_bp=line, legal=legal, anchor_ratio=cfg.anchor_ratio
        )
        if anchor is None:
            return settle("no_lower_counter", over_line_reason="no_legal_counter")
        if a.firm:
            final = step_bp(prev=anchor, raw=(anchor + ask_bp) // 2, ceiling=ask_bp, legal=legal)
            return counter(
                final if final is not None else anchor,
                0,
                final_ask=ask_bp,
                reason="final_counter",
                template=_FINAL_COUNTER_TEMPLATE,
            )
        return counter(anchor, 0)

    # They repeated (or bettered) their number after our final counter: accept.
    if neg.final_counter_ask is not None and ask_bp <= neg.final_counter_ask:
        return settle("rep_firm")
    if a.firm:
        if ask_bp > line:
            return _handoff(t.effects, "above_accept_line")
        if len(neg.counter_turns) >= cfg.max_counters:
            return settle("counters_exhausted", over_line_reason="max_counters")
        final = step_bp(prev=c_prev, raw=(c_prev + ask_bp) // 2, ceiling=ask_bp, legal=legal)
        if final is None:
            return settle("rep_firm")
        return counter(
            final, 0, final_ask=ask_bp, reason="final_counter", template=_FINAL_COUNTER_TEMPLATE
        )
    if len(neg.counter_turns) >= cfg.max_counters:
        return settle("counters_exhausted", over_line_reason="max_counters")

    target = min(ask_bp, line)
    moved = neg.ask_at_last_counter is not None and ask_bp < neg.ask_at_last_counter
    if moved:
        assert neg.ask_at_last_counter is not None
        give = int(
            Decimal(str(cfg.concession_factor)) * Decimal(neg.ask_at_last_counter - ask_bp)
        )
        nxt = step_bp(prev=c_prev, raw=c_prev + give, ceiling=min(ask_bp, line + 1), legal=legal)
        if nxt is None or nxt > c_prev + give:
            # Half their move does not reach the next legal bp: restate our offer.
            return counter(c_prev, 0, reason="hold", template=_HOLD_TEMPLATE)
        return counter(nxt, 0)

    stage = neg.hold_stage
    if stage == 0:
        return counter(c_prev, 1, reason="hold", template=_HOLD_TEMPLATE)
    if stage < _LAST_HOLD_STAGE:
        raw = c_prev + max(0, target - c_prev) // _HOLD_STEP_DIVISOR
        nxt = step_bp(prev=c_prev, raw=raw, ceiling=min(ask_bp, line + 1), legal=legal)
        if nxt is not None:
            return counter(nxt, stage + 1, reason="step")
    return settle("rep_held")


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


# What the agent says when it hands off (no digits). The reason code goes to
# the person taking over; ``app.agent.reasons`` explains each one.
HANDOFF_SPOKEN: dict[str, str] = {
    "max_turns": "We have gone back and forth for a while, so a specialist will take this over.",
    "infeasible": (
        "No payment schedule fits the client's program under these terms, "
        "so a specialist will review it."
    ),
    "max_counters": (
        "We have made every offer we can on this call, so a specialist will take it from here."
    ),
    "no_legal_counter": (
        "We cannot propose a settlement below your number under these terms, "
        "so a specialist will review it."
    ),
    "above_accept_line": (
        "Your number is above what I can accept on this call, so a specialist will review it."
    ),
    "confirm_unacked": (
        "We have not been able to confirm a schedule, so a specialist will follow up."
    ),
    "confirm_rejected": (
        "We could not find a schedule that works for both sides, so a specialist will follow up."
    ),
    "repeated_question": (
        "We keep coming back to the same question, so a specialist will sort it out."
    ),
    "no_progress": "We do not seem to be moving forward, so a specialist will take this over.",
}
# The rep is leaving: no "I need to involve someone" lead-in, just the follow-up.
_FOLLOW_UP_TEMPLATE = "Understood. A specialist from our side will follow up with you."


def _handoff(effects: list[Effect], reason: str) -> Action:
    """ESCALATE for a Phase 45 handoff code (``HANDOFF_SPOKEN``)."""
    return _escalate(effects, reason=reason, spoken_reason=HANDOFF_SPOKEN[reason])


def follow_up_action(
    effects: list[Effect] | None = None, *, reason: str = "wants_to_end"
) -> Action:
    """Hand off when the rep wants to end without a deal: a specialist follows up."""
    return Action(
        intent=Intent.ESCALATE,
        text_slots={"escalate_reason": "A specialist from our side will follow up with you."},
        effects=list(effects or [])
        + [Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value})],
        next_phase=Phase.ESCALATE,
        reason=reason,
        template_override=_FOLLOW_UP_TEMPLATE,
    )


# Moves that ask the rep something; the loop guard counts them per key.
_QUESTION_INTENTS = frozenset(
    {Intent.ASK, Intent.ASK_SETTLEMENT, Intent.CLARIFY, Intent.READ_BACK}
)


def question_key(action: Action) -> str | None:
    """``INTENT:reason`` for a question move (same intent + field / template), else None."""
    if action.intent not in _QUESTION_INTENTS:
        return None
    return f"{action.intent.value}:{action.reason or ''}"


def loop_guard(action: Action, neg: NegotiationState, cfg: Settings) -> Action:
    """Hand off instead of asking the same question a third time; else count this ask.

    The count is an ``note_question`` effect, applied when the move is spoken.
    """
    key = question_key(action)
    # ``_decide_wrap`` re-enters ``decide``; count each spoken question once.
    if key is None or any(e.kind == "note_question" for e in action.effects):
        return action
    if neg.question_counts.get(key, 0) >= cfg.max_same_question:
        kept = [e for e in action.effects if e.kind in ("record_ask", "clear_pending_terms_alt")]
        return _handoff(kept, "repeated_question")
    return action.model_copy(
        update={"effects": [*action.effects, Effect(kind="note_question", data={"key": key})]}
    )


def rep_turn_progress(
    analysis: TurnAnalysis,
    neg: NegotiationState,
    belief_changes: list[BeliefChange],
    *,
    accepted_term_alt: bool = False,
) -> bool:
    """True when this rep turn added verified information or moved the call.

    Progress: a belief value or status changed, a new ask, an accept or reject,
    a read-back answer, a firm stance, or a yes to a non-price alternative.
    A restated ask, a repeated term, a question or small talk is not progress.
    """
    if any(
        c.old_value != c.new_value or c.old_status != c.new_status for c in belief_changes
    ):
        return True
    if (
        analysis.settlement_ask_pct is not None
        and ask_pct_to_bp(analysis.settlement_ask_pct) != neg.ask_bp
    ):
        return True
    return (
        analysis.stance in ("accept", "reject")
        or analysis.readback_response in ("confirm", "deny")
        or analysis.firm
        or accepted_term_alt
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
    action: Action | None = None
    for step in (_decide_interruptions, _decide_clarify, _decide_confirm, _decide_discovery):
        action = step(t)
        if action is not None:
            break
    if action is None:
        action = _decide_negotiate(t)
    return loop_guard(action, neg, t.cfg)


def _decide_interruptions(t: _Turn) -> Action | None:
    """Moves that preempt the phase flow, in priority order."""
    a, neg, cfg = t.analysis, t.neg, t.cfg
    if neg.turn_idx > cfg.max_turns:
        return _handoff(t.effects, "max_turns")
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
    if neg.no_progress_turns >= cfg.max_no_progress_turns:
        return _handoff(t.effects, "no_progress")
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
        return follow_up_action(t.effects)
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
    """Ambiguous tiers, then CONTRADICTED (CLARIFY, escalate after two), then TENTATIVE."""
    belief = t.belief
    if t.analysis.tiers_ambiguous:
        if t.neg.clarify_counts.get("min_payment_tiers", 0) >= 2:
            return _escalate(
                t.effects,
                reason="tiers_unresolved",
                spoken_reason="I need a specialist to confirm the payment tiers.",
            )
        return Action(
            intent=Intent.CLARIFY,
            effects=t.effects
            + [Effect(kind="note_clarify", data={"field": "min_payment_tiers"})],
            next_phase=Phase.DISCOVERY,
            reason="tiers_ambiguous",
            template_override=(
                "Just to be sure on the payment tiers: from which payment number "
                "does each higher minimum start, and what is that minimum?"
            ),
        )
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
        old_s = _spoken_value("clarify_old", fname, old, facts, slots, required)
        new_s = _spoken_value("clarify_new", fname, term.value, facts, slots, required)
        is_tiers = FIELDS_BY_NAME[fname].kind == "tiers"
        return Action(
            intent=Intent.CLARIFY,
            facts=facts,
            text_slots=slots,
            required=required,
            effects=t.effects + [Effect(kind="note_clarify", data={"field": fname})],
            next_phase=Phase.DISCOVERY,
            reason=fname,
            template_override=(
                f"Earlier you mentioned {old_s}, and now I am hearing {new_s}. "
                "Which of those should I use?"
                if is_tiers
                else None
            ),
        )
    tentative = belief.tentative_fields()
    if tentative:
        fname = tentative[0]
        facts = {}
        slots = {"field_label": FIELDS_BY_NAME[fname].label}
        required = set()
        value = belief.get(fname).value
        spoken = _spoken_value("readback_value", fname, value, facts, slots, required)
        effects = t.effects + [Effect(kind="set_pending_readback", data={"field": fname})]
        # A READ_BACK mid-CONFIRM keeps the phase, and an accept it preempted is
        # remembered so the readback "yes" wraps instead of reopening the ladder.
        neg, a = t.neg, t.analysis
        in_confirm = neg.phase == Phase.CONFIRM
        same_pct = a.settlement_ask_pct is None or (
            ask_pct_to_bp(a.settlement_ask_pct) == neg.confirmed_bp
        )
        if in_confirm and a.stance == "accept" and same_pct and neg.last_confirm_key:
            effects.append(
                Effect(kind="note_confirm_accepted", data={"key": list(neg.last_confirm_key)})
            )
        is_tiers = FIELDS_BY_NAME[fname].kind == "tiers"
        return Action(
            intent=Intent.READ_BACK,
            facts=facts,
            text_slots=slots,
            required=required,
            effects=effects,
            next_phase=Phase.CONFIRM if in_confirm else Phase.DISCOVERY,
            reason=fname,
            template_override=f"So I have {spoken}. Is that right?" if is_tiers else None,
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
    Exception: a confirmed READ_BACK after an accept it preempted
    (``accepted_confirm_key``) wraps if the fingerprint is unchanged, else
    re-confirms the accepted bp under the revised terms (never re-ladders).
    """
    a, neg = t.analysis, t.neg
    if neg.phase != Phase.CONFIRM or neg.confirmed_bp is None or neg.last_confirm_key is None:
        return None
    if (
        a.readback_response == "confirm"
        and neg.accepted_confirm_key == neg.last_confirm_key
        and a.settlement_ask_pct is None
        and a.stance not in ("reject", "counter", "offer")
    ):
        if _confirm_key(t.belief, neg.confirmed_bp) == neg.last_confirm_key:
            return _propose_wrap(t)
        afford = t.afford
        if (
            afford is not None
            and afford.max_bp is not None
            and neg.confirmed_bp <= accept_line_bp(afford.max_bp, t.cfg.accept_line_pct_of_max_bp)
            and neg.confirmed_bp in afford.feasible_bps
        ):
            return _confirm_action(
                ask_bp=neg.confirmed_bp,
                belief=t.belief,
                confirm_facts=t.confirm_facts,
                effects=t.effects,
                required=_confirm_required(t.confirm_facts),
                reason="terms_revised",
            )
    if a.stance != "accept" or t.accepted_term_alt:
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
        return _handoff(t.effects, "infeasible")
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
    return _negotiate(t, ask_bp, afford)


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
    """Rep accepted one of our offers: confirm it, if still feasible and at or below the line.

    A restated % counts only when it is a number we offered; any other number
    is the rep's own ask and goes through the ladder (never accepted first —
    Phase 45, the A/B pair-17 "100% balance" accept). A yes to a term alt is
    not a yes to the previous price — that counter can be a token percent the
    new terms just made obsolete.
    """
    a, neg = t.analysis, t.neg
    if a.stance != "accept":
        return None
    assert afford.max_bp is not None
    line = accept_line_bp(afford.max_bp, t.cfg.accept_line_pct_of_max_bp)
    legal = {bp for bp in afford.feasible_bps if bp <= line}
    ours = set(neg.counters_offered)
    if neg.confirmed_bp is not None:
        ours.add(neg.confirmed_bp)
    bp: int | None = None
    if a.settlement_ask_pct is not None:
        stated = ask_pct_to_bp(a.settlement_ask_pct)
        if stated not in ours:
            return None
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
        and confirmed <= accept_line_bp(afford.max_bp, t.cfg.accept_line_pct_of_max_bp)
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
        return _handoff(soft, "confirm_unacked")
    return _confirm_action(
        ask_bp=confirmed,
        belief=t.belief,
        confirm_facts=t.confirm_facts,
        effects=soft,
        required=set(),
    )


def _stall_after_confirm(t: _Turn) -> Action:
    """Rejected identical CONFIRM: verify each ASSUMED field once, else hand off."""
    effects = t.effects + [Effect(kind="inc_confirm_reject")]
    fname = _first_assumed_field(t.belief, already_asked=t.neg.assumed_asked)
    if fname is not None:
        return _ask_field(
            fname, effects + [Effect(kind="note_assumed_asked", data={"field": fname})]
        )
    return _handoff(effects, "confirm_rejected")


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
