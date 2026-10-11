"""Move builders: turn a decision into a fully-formed ``Action``.

Each builder picks the intent, reason code, PUBLIC facts (spoken numbers only
ever come from these facts) and the state ``effects`` for one kind of move:
asking a field, reading back or clarifying a value (incl. "dollars or
cents?" and "total or per payment?"), confirming a schedule, proposing a
non-price alternative, the opening, and handoffs (``HANDOFF_SPOKEN`` is the
spoken line per handoff code; the code itself stays operator-only). Builders
do not decide which move to make: that is ``cascade`` and ``ladder``.
"""

from __future__ import annotations

from datetime import date
from string import ascii_lowercase
from typing import Any

from app.agent.policy.state import terms_counter_key
from app.config import Settings, get_settings
from app.domain.actions import Action, Effect, Intent, Phase
from app.domain.belief import BeliefState, TermStatus
from app.domain.facts import Fact
from app.domain.fields import FIELD_REGISTRY, FIELDS_BY_NAME
from feasibility.engine import ScheduleRow


def _counter_terms_action(
    *,
    field: str,
    value: Any,
    effects: list[Effect],
    rep_stated: bool = True,
) -> Action:
    """Build a field-aware COUNTER_TERMS move with the right Fact + template.

    ``rep_stated`` is false when the rep never gave this term (our belief is an
    ASSUMED default): then we only propose our value as a question and
    never say "that ... does not fit", which would object to words they never said.
    """
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
            if rep_stated
            else "Could payment start on {alt_first_payment_date}?"
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
            if rep_stated
            else "Could you allow a minimum payment of {alt_min_payment_cents}?"
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
            if rep_stated
            else "Could you allow up to {alt_max_payments} payments?"
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


# ``clarify_counts`` key for the total-vs-per-payment question.
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
    """ESCALATE for a no-deal handoff code (``HANDOFF_SPOKEN``)."""
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


def _escalate(effects: list[Effect], *, reason: str, spoken_reason: str) -> Action:
    """Hand off to a human and move to ESCALATE (no digits in the reason)."""
    return Action(
        intent=Intent.ESCALATE,
        text_slots={"escalate_reason": spoken_reason},
        effects=effects + [Effect(kind="set_phase", data={"phase": Phase.ESCALATE.value})],
        next_phase=Phase.ESCALATE,
        reason=reason,
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
