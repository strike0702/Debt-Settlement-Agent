"""Plain-English explanations of policy reason codes, for the decision trace UI.

``decide()`` (and a few orchestrator-built moves) set ``Action.reason`` to a
machine code: a literal (``"max_counters"``), a field name (``ASK`` /
``READ_BACK`` / ``CLARIFY``), ``"bp=4900"`` (COUNTER / CONFIRM), or ``None``.
``reason_key`` folds those into the keys of ``REASON_TEXT``; ``reason_text``
fills the sentence's ``{placeholders}`` from the action's PUBLIC facts only.

This is display text for the operator and rep panels, not speech: it never
goes through NLG and never changes a decision. The private ceiling
(``max_bp``) and client financials are never placeholders, so the same text is
safe on the rep stream. Called by ``orchestrator`` when it builds the trace.
"""

from __future__ import annotations

import string
from datetime import date

from app.domain.actions import Action, Intent
from app.domain.fields import FIELDS_BY_NAME

# Every placeholder a REASON_TEXT sentence may use. All are values the agent
# speaks (PUBLIC facts) or field labels; tests assert nothing else appears.
PUBLIC_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "counter_pct",
        "settlement_pct",
        "offer_total",
        "num_payments",
        "first_payment_date",
        "alt_first_payment_date",
        "alt_min_payment_cents",
        "alt_max_payments",
        "field_label",
    }
)

REASON_TEXT: dict[str, str] = {
    # Opening and discovery
    "opening": (
        "Open with the required disclosure and ask what terms the creditor can work with."
    ),
    "ask_field": "Ask for the {field_label}: the engine needs it before it can check any schedule.",
    "ask_settlement": (
        "Ask for the settlement percentage: the required rules are known, so price comes next."
    ),
    "read_back": (
        "Read back the {field_label}: the rep's statement was hedged or did not match "
        "their words exactly."
    ),
    "clarify_field": "Ask which {field_label} is right: the rep has given two different values.",
    "tiers_ambiguous": (
        "Ask the rep to restate the payment tiers: the wording does not say where each tier starts."
    ),
    "cents_ambiguity": "Ask whether the bare amount means dollars or cents before using it.",
    # Price moves
    "counter": (
        "Counter at {counter_pct} ({offer_total}): the next step of the concession ladder, "
        "which anchors below the ask and never goes past what the client can afford."
    ),
    # Display variant (not a reason code): the engine returned no offer total.
    "counter_no_total": (
        "Counter at {counter_pct}: the next step of the concession ladder, "
        "which anchors below the ask and never goes past what the client can afford."
    ),
    "confirm": (
        "Confirm at {settlement_pct}: the engine finds this schedule feasible for the client, "
        "so the agent reads the terms back for a yes."
    ),
    "ask_within_offer": (
        "Confirm at {settlement_pct}: the rep's ask is at or below an offer the agent already made."
    ),
    "rep_firm": (
        "Confirm at {settlement_pct}: the rep is firm and the client can afford it, "
        "so the agent stops countering."
    ),
    "counters_exhausted": (
        "Confirm at {settlement_pct}: the agent has used all its counters "
        "and the ask is affordable."
    ),
    "no_lower_counter": (
        "Confirm at {settlement_pct}: there is no feasible counter below the rep's ask."
    ),
    "ladder_stalled": (
        "Confirm at {settlement_pct}: the next counter would not improve on the last one."
    ),
    "gap_small": (
        "Confirm at {settlement_pct}: the gap to the ask is too small for another counter."
    ),
    "terms_revised": (
        "Re-confirm at {settlement_pct}: the rules changed after the last confirmation."
    ),
    # Non-price alternatives
    "alt_first_payment_date": (
        "Propose a first payment on {alt_first_payment_date}: the requested start date "
        "leaves no affordable schedule."
    ),
    "alt_min_payment_cents": (
        "Propose a lower minimum of {alt_min_payment_cents}: the current minimum blocks "
        "an affordable schedule."
    ),
    "alt_max_payments": (
        "Propose up to {alt_max_payments} payments: more payments let the client afford "
        "a better offer."
    ),
    # Wrap
    "confirmed": (
        "Send the proposal to the client for approval: the rep accepted the confirmed schedule."
    ),
    "thanks_accept": (
        "Close with thanks: the rep accepted and the proposal goes to the client for approval."
    ),
    "post_wrap": "Close politely: the proposal has already gone to the client.",
    "already_ended": "Close politely: the call has already ended.",
    "schedule_detail": "Read out the payment schedule: the rep asked for the details.",
    "schedule_detail_post_wrap": (
        "Read out the proposed payment schedule: the rep asked for it after the proposal."
    ),
    "wrap_renegotiate": "Reopen the deal: the rep changed the terms after the proposal.",
    "rep_ended": "End the call: the rep ended the chat.",
    "rep_ended_after_wrap": "Close: the rep ended the chat after the proposal was sent.",
    # Guardrails
    "private_info": "Decline: the rep asked for the client's private financial information.",
    "sensitive_request": (
        "Escalate: the rep asked again for private client information after a refusal."
    ),
    "commitment": "Decline to commit: only the client can approve a settlement.",
    "commitment_demand": "Escalate: the rep keeps demanding a binding commitment on the call.",
    "hostile": "Escalate: the conversation crossed the hostility threshold.",
    "contradiction_unresolved": (
        "Escalate: two clarifications did not resolve the conflicting terms."
    ),
    "tiers_unresolved": "Escalate: the payment tiers stayed unclear after two clarifications.",
    "already_escalated": "Stay escalated: a specialist still needs to join the call.",
    "out_of_guardrail": (
        "Escalate: no schedule fits these rules, and closing the gap needs extra client "
        "funds that only the client can approve."
    ),
    # No deal
    "infeasible": (
        "End without a deal: no schedule fits the client's program under these rules, "
        "and no alternative term helps."
    ),
    "no_legal_counter": (
        "End without a deal: there is no affordable percentage below the ask to offer."
    ),
    "max_counters": (
        "End without a deal: the best offer the client can afford is already on the table "
        "and the rep did not take it."
    ),
    "confirm_unacked": "End without a deal: the rep never confirmed the proposed schedule.",
    "confirm_rejected": (
        "End without a deal: the rep rejected the schedule and every assumed term was checked."
    ),
    "max_turns": "End the call: it reached the turn limit without an agreement.",
    "wants_to_end": "End the call: the rep wants to stop and has not accepted.",
}

# Intent-level fallback when a code is unknown (new code without an entry).
_GENERIC = "The policy chose {intent}."
_MISSING_VALUE = "that value"

# ``reason=None`` → key by intent; any other intent falls through to the generic line.
_NONE_REASON_KEY: dict[Intent, str] = {
    Intent.OPENING: "opening",
    Intent.ASK_SETTLEMENT: "ask_settlement",
    Intent.CONFIRM_SCHEDULE: "confirm",
    Intent.COUNTER: "counter",
}
_FIELD_REASON_KEY: dict[Intent, str] = {
    Intent.ASK: "ask_field",
    Intent.READ_BACK: "read_back",
    Intent.CLARIFY: "clarify_field",
}


def reason_key(intent: Intent | str, reason: str | None) -> str:
    """Fold a raw reason code into a ``REASON_TEXT`` key (may be absent for new codes)."""
    intent = Intent(intent)
    if reason is None:
        return _NONE_REASON_KEY.get(intent, intent.value.lower())
    if reason.startswith("bp="):
        return "counter" if intent == Intent.COUNTER else "confirm"
    if reason in FIELDS_BY_NAME and intent in _FIELD_REASON_KEY:
        return _FIELD_REASON_KEY[intent]
    return reason


class _Fill(string.Formatter):
    """``str.format`` that substitutes a neutral word for a missing value."""

    def get_value(self, key: int | str, args: object, kwargs: dict[str, str]) -> str:  # type: ignore[override]
        return kwargs.get(str(key), _MISSING_VALUE)


def reason_text(action: Action, ref: date) -> str:
    """One plain-English sentence for ``action.reason``, filled from PUBLIC facts only."""
    key = reason_key(action.intent, action.reason)
    if key == "counter" and "offer_total" not in action.facts:
        key = "counter_no_total"
    template = REASON_TEXT.get(key)
    if template is None:
        return _GENERIC.format(intent=action.intent.value.replace("_", " ").lower())
    values: dict[str, str] = {
        fid: fact.render(ref)
        for fid, fact in action.facts.items()
        if fact.visibility == "PUBLIC" and fid in PUBLIC_PLACEHOLDERS
    }
    spec = FIELDS_BY_NAME.get(action.reason or "")
    label = action.text_slots.get("field_label") or (spec.label if spec else None)
    if label:
        values["field_label"] = label
    return _Fill().format(template, **values)
