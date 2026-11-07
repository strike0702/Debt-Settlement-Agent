"""Move builders: turn a decision into a fully-formed ``Action``.

Each builder picks the intent, reason code, PUBLIC facts (spoken numbers only
ever come from these facts) and the state ``effects`` for one kind of move:
asking a field, reading back or clarifying a value (incl. "dollars or
cents?" and "total or per payment?"), confirming a schedule, proposing a
non-price alternative, the rep-defined tier asks / whole-set read-back /
tier conflict clarify, the optional-rules sweep, the opening, and handoffs
(``HANDOFF_SPOKEN`` is the spoken line per handoff code; the code itself stays
operator-only). Builders do not decide which move to make: that is
``cascade`` and ``ladder``.
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


def yield_ask_action(fname: str, effects: list[Effect]) -> Action:
    """ASK a field the rep just said they want to define ("Go ahead." + its question)."""
    action = _ask_field(fname, effects)
    # The field's own question stays in the line, so a bare answer still takes
    # the fast path (``fast_paths._field_for_ask_line``).
    slots = {**action.text_slots, "ask_text": f"Go ahead. {FIELDS_BY_NAME[fname].ask_text}"}
    return action.model_copy(update={"text_slots": slots})


_TIERS_FIELD = "min_payment_tiers"
# Guided asks while the rep gives tiers one by one (figure-free).
_TIER_QUESTION = "from which payment does it start, and what is its minimum?"
_TIER_ASKS: dict[str, str] = {
    "first": f"Go ahead. For each tier, {_TIER_QUESTION}",
    # Counts are Facts, never words in the template (the guard blocks those).
    "first_counted": f"Got it, {{tier_count}} tiers. Starting with tier {{tier_position}}: "
    f"{_TIER_QUESTION}",
    "next": f"Got it. And the next tier: {_TIER_QUESTION}",
    "more": f"Got it. Is there another tier? If so, {_TIER_QUESTION}",
    "again": f"Let us go through the tiers again. For each tier, {_TIER_QUESTION}",
}


def tier_draft_effect(
    tiers: list[tuple[int, int]] | None,
    expected: int | None,
    *,
    readback: bool = False,
    ends: dict[int, int] | None = None,
) -> Effect:
    """``set_tier_draft``: the tiers merged so far (None ends the collection).

    ``ends`` maps a drafted tier's first payment to its last, when the rep said it.
    """
    data: dict[str, Any] = {
        "tiers": None if tiers is None else [list(t) for t in tiers],
        "expected": expected,
        "readback": readback,
    }
    if ends:
        data["ends"] = {str(k): v for k, v in sorted(ends.items())}
    return Effect(kind="set_tier_draft", data=data)


def tier_ask_action(
    *,
    draft: list[tuple[int, int]],
    expected: int | None,
    effects: list[Effect],
    again: bool = False,
) -> Action:
    """ASK for the first / next tier; ``effects`` must carry the ``set_tier_draft``."""
    if again:
        stage = "again"
    elif draft:
        stage = "next" if expected is not None else "more"
    else:
        stage = "first_counted" if expected is not None else "first"
    spec = FIELDS_BY_NAME["min_payment_tiers"]
    facts: dict[str, Fact] = {}
    if stage == "first_counted":
        assert expected is not None
        facts = {
            "tier_count": Fact(
                id="tier_count", kind="count", value=expected, visibility="PUBLIC",
                source="creditor",
            ),
            "tier_position": Fact(
                id="tier_position", kind="count", value=1, visibility="PUBLIC", source="config"
            ),
        }
    return Action(
        intent=Intent.ASK,
        facts=facts,
        text_slots={"ask_text": _TIER_ASKS[stage], "field_label": spec.label},
        effects=effects,
        next_phase=Phase.DISCOVERY,
        reason=_TIERS_FIELD,
        template_override=_TIER_ASKS[stage],
    )


def tier_readback_action(
    *,
    tiers: list[tuple[int, int]],
    max_payments: int | None,
    effects: list[Effect],
) -> Action:
    """READ_BACK the whole tier set once, as payment ranges.

    "So payments {a_from} to {a_to} at least {a_min}, {b_from} to {b_to} at
    least {b_min}. Is that right?" A tier runs to the payment before the next
    one starts; the last runs to ``max_payments`` (or "on" when unknown). Ids
    use the ``readback_value_tier_<letter>`` prefix the sim reads back.
    """
    facts: dict[str, Fact] = {}
    parts: list[str] = []
    for i, (tag, (frm, cents)) in enumerate(zip(ascii_lowercase, tiers, strict=False)):
        base = f"readback_value_tier_{tag}"
        nxt = tiers[i + 1][0] - 1 if i + 1 < len(tiers) else max_payments
        facts[f"{base}_from"] = Fact(
            id=f"{base}_from", kind="count", value=frm, visibility="PUBLIC", source="creditor"
        )
        facts[f"{base}_min"] = Fact(
            id=f"{base}_min", kind="money", value=cents, visibility="PUBLIC", source="creditor"
        )
        noun = "payments " if i == 0 else ""
        if nxt is None or nxt < frm:
            part = f"from payment {{{base}_from}} on"
        elif nxt == frm:
            part = f"payment {{{base}_from}}"
        else:
            facts[f"{base}_to"] = Fact(
                id=f"{base}_to", kind="count", value=nxt, visibility="PUBLIC", source="creditor"
            )
            part = f"{noun}{{{base}_from}} to {{{base}_to}}"
        parts.append(f"{part} at least {{{base}_min}}")
    return Action(
        intent=Intent.READ_BACK,
        facts=facts,
        text_slots={"field_label": FIELDS_BY_NAME["min_payment_tiers"].label},
        required=set(facts),
        effects=effects,
        next_phase=Phase.DISCOVERY,
        reason=_TIERS_FIELD,
        template_override=f"So {', '.join(parts)}. Is that right?",
    )


def tier_conflict(
    tiers: list[tuple[int, int]], *, max_payments: int | None, min_payment_cents: int | None
) -> tuple[str, tuple[int, int]] | None:
    """First tier that contradicts a known rule: ``("past_max" | "below_min", tier)``."""
    for frm, cents in tiers:
        if max_payments is not None and frm > max_payments:
            return "past_max", (frm, cents)
        if min_payment_cents is not None and cents < min_payment_cents:
            return "below_min", (frm, cents)
    return None


def tier_conflict_clarify_action(
    kind: str, tier: tuple[int, int], *, rule_value: int, effects: list[Effect]
) -> Action:
    """CLARIFY a tier against the max payments / minimum payment the rep gave.

    Counts as a ``min_payment_tiers`` clarify, so the existing two-clarify
    limit hands off (``tiers_unresolved``). The caller drops the tier from the
    draft, so the rep's answer re-gives it.
    """
    frm, cents = tier
    if kind == "past_max":
        old = Fact(id="clarify_old", kind="count", value=rule_value, visibility="PUBLIC",
                   source="creditor")  # fmt: skip
        new = Fact(id="clarify_new", kind="count", value=frm, visibility="PUBLIC",
                   source="creditor")  # fmt: skip
        template = (
            "Just to check: you said at most {clarify_old} payments, but that tier starts "
            "at payment {clarify_new}. Which is right?"
        )
    else:
        old = Fact(id="clarify_old", kind="money", value=rule_value, visibility="PUBLIC",
                   source="creditor")  # fmt: skip
        new = Fact(id="clarify_new", kind="money", value=cents, visibility="PUBLIC",
                   source="creditor")  # fmt: skip
        template = (
            "Just to check: you said the minimum payment is {clarify_old}, but that tier's "
            "minimum is {clarify_new}. Which minimum applies to those payments?"
        )
    return Action(
        intent=Intent.CLARIFY,
        facts={"clarify_old": old, "clarify_new": new},
        text_slots={"field_label": FIELDS_BY_NAME["min_payment_tiers"].label},
        required={"clarify_old", "clarify_new"},
        effects=effects + [Effect(kind="note_clarify", data={"field": "min_payment_tiers"})],
        next_phase=Phase.DISCOVERY,
        reason="tiers_inconsistent",
        template_override=template,
    )


SWEEP_ASK_TEXT = "Any tiered minimums, or a limit on payment levels?"


def sweep_action(effects: list[Effect]) -> Action:
    """The one optional-rules question before the price (flexible schedules only)."""
    return Action(
        intent=Intent.ASK,
        text_slots={"ask_text": SWEEP_ASK_TEXT, "field_label": "payment tiers or levels"},
        effects=effects + [Effect(kind="note_sweep", data={})],
        next_phase=Phase.DISCOVERY,
        reason="optional_sweep",
    )


def rephrased_ask(action: Action) -> Action:
    """Same question, other words: never the same line twice when the rep said something else."""
    if action.intent == Intent.ASK_SETTLEMENT:
        template = (
            "Sure, go ahead. When you are ready, what percentage of the balance "
            "would you settle for?"
        )
    else:
        template = "Sure, go ahead. When you are ready, I still need the {field_label}."
    return action.model_copy(update={"template_override": template})


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
