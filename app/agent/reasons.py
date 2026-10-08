"""Plain-English explanations of policy reason codes, for the decision trace UI.

``decide()`` (and a few orchestrator-built moves) set ``Action.reason`` to a
machine code: a literal (``"max_counters"``), a field name (``ASK`` /
``READ_BACK`` / ``CLARIFY``), ``"bp=4900"`` (COUNTER / CONFIRM), or ``None``.
``reason_key`` folds those into the keys of ``REASON_TEXT``; ``reason_text``
fills the sentence's ``{placeholders}`` from the action's PUBLIC facts only.
``REASON_SHORT`` / ``reason_short`` give the same reason in a few words with no
numbers, for the trace's collapsed rows (added at emit time by ``app.voice.ws``).

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

# One full sentence per reason, written for a newcomer reading the trace ("we"
# is the agent). Phase 36 rewrote the wording only: keys, placeholders and
# which reason ``decide()`` picks are unchanged.
REASON_TEXT: dict[str, str] = {
    # Opening and discovery
    "opening": (
        "We open by saying who we are and that this is an automated call, as required, "
        "then ask what payment terms the creditor can accept."
    ),
    "ask_field": (
        "We ask for the {field_label}, because we cannot check whether any payment plan "
        "works for the client without it."
    ),
    "ask_settlement": (
        "We now know all the payment rules, so the next step is to ask what percentage "
        "of the balance they would settle for."
    ),
    "read_back": (
        "We repeat the {field_label} back to the rep to confirm it, because they sounded "
        "unsure or their words did not clearly match what we heard."
    ),
    "clarify_field": (
        "The rep has given two different values for the {field_label}, so we ask which one "
        "is right."
    ),
    "tiers_ambiguous": (
        "We ask the rep to restate their minimum payments, because their wording does not "
        "say which payment each minimum starts from."
    ),
    "cents_ambiguity": (
        "The rep said a bare number, so we ask whether it means dollars or cents before "
        "using it."
    ),
    "amount_meaning": (
        "The rep named a dollar amount that could be the total settlement or the minimum "
        "for each payment, so we ask which one before using it."
    ),
    # Price moves
    "counter": (
        "We offer {counter_pct} ({offer_total}). We start below their ask and move up in "
        "small steps, and we never offer more than the client can afford."
    ),
    # Display variant (not a reason code): the engine returned no offer total.
    "counter_no_total": (
        "We offer {counter_pct}. We start below their ask and move up in small steps, "
        "and we never offer more than the client can afford."
    ),
    "confirm": (
        "We agree to {settlement_pct} because the client can afford a payment plan at that "
        "level, and we repeat the terms so the rep can say yes."
    ),
    "ask_within_offer": (
        "We agree to {settlement_pct} because the rep is now asking for no more than we "
        "already offered."
    ),
    "rep_firm": (
        "We agree to {settlement_pct}: the rep will not go lower and the client can afford "
        "it, so there is no reason to keep bargaining."
    ),
    "counters_exhausted": (
        "We agree to {settlement_pct} because we have made every counteroffer we are "
        "allowed, and the client can afford their ask."
    ),
    "no_lower_counter": (
        "We agree to {settlement_pct} because there is no lower percentage the client "
        "could afford to offer instead."
    ),
    "ladder_stalled": (
        "We agree to {settlement_pct} because our next counteroffer would be no better "
        "than the last one."
    ),
    "gap_small": (
        "We agree to {settlement_pct} because we are already so close to their ask that "
        "another counteroffer is not worth it."
    ),
    "terms_revised": (
        "We confirm {settlement_pct} again because the payment rules changed after we "
        "last confirmed it."
    ),
    # Non-price alternatives
    "alt_first_payment_date": (
        "We suggest a first payment on {alt_first_payment_date}, because with the start "
        "date they asked for, no payment plan fits the client's savings."
    ),
    "alt_min_payment_cents": (
        "We suggest a lower minimum payment of {alt_min_payment_cents}, because their "
        "current minimum makes every plan unaffordable for the client."
    ),
    "alt_max_payments": (
        "We suggest allowing up to {alt_max_payments} payments, because spreading the "
        "payments out lets the client afford a better offer."
    ),
    # Wrap
    "confirmed": "The rep agreed to the payment plan, so we send it to the client to approve.",
    "thanks_accept": (
        "The rep has agreed and the proposal is going to the client, so we thank them and "
        "close the call."
    ),
    "post_wrap": "The proposal has already gone to the client, so we close the call politely.",
    "already_ended": "The call has already ended, so we close politely.",
    "schedule_detail": "The rep asked for the details, so we read out the payment schedule.",
    "schedule_detail_post_wrap": (
        "The rep asked to hear the payment schedule after the proposal was sent, so we "
        "read it out."
    ),
    "wrap_renegotiate": (
        "The rep changed the terms after we sent the proposal, "
        "so we reopen the deal."
    ),
    "rep_ended": "The rep ended the conversation, so we end the call.",
    "rep_ended_after_wrap": (
        "The rep ended the conversation after the proposal was sent, so we close the call."
    ),
    # Guardrails
    "private_info": (
        "We decline, because the rep asked about the client's private finances, which we "
        "never share."
    ),
    "sensitive_request": (
        "We hand the call to a person, because the rep asked again for the client's private "
        "information after we said no."
    ),
    "commitment": "We do not commit on the call, because only the client can approve a settlement.",
    "commitment_demand": (
        "We hand the call to a person, because the rep keeps insisting on a binding promise "
        "during the call."
    ),
    "hostile": "We hand the call to a person, because the conversation became too hostile.",
    "contradiction_unresolved": (
        "We hand the call to a person, because asking twice did not clear up the "
        "conflicting terms."
    ),
    "tiers_unresolved": (
        "We hand the call to a person, because the minimum payment rules were still unclear "
        "after we asked twice."
    ),
    "amount_meaning_unresolved": (
        "We hand the call to a person, because after we asked twice it was still unclear "
        "whether the rep's dollar amount was the total settlement or the minimum per payment."
    ),
    "already_escalated": (
        "We wait for a person to take over, "
        "because a specialist still needs to join the call."
    ),
    "out_of_guardrail": (
        "We hand the call to a person, because no payment plan fits these rules unless the "
        "client adds money, and only the client can agree to that."
    ),
    # No deal
    "infeasible": (
        "We end without a deal, because no payment plan fits the client's savings under "
        "these rules, and changing a term would not help."
    ),
    "no_legal_counter": (
        "We end without a deal, because there is no percentage below their ask that the "
        "client can afford."
    ),
    "max_counters": (
        "We end without a deal, because our best affordable offer is already on the table "
        "and the rep turned it down."
    ),
    "confirm_unacked": (
        "We end without a deal, "
        "because the rep never agreed to the proposed schedule."
    ),
    "confirm_rejected": (
        "We end without a deal, because the rep rejected the schedule and we had already "
        "checked every term we had assumed."
    ),
    "max_turns": "We end the call, because it went on too long without an agreement.",
    "wants_to_end": "We end the call, because the rep wants to stop and has not agreed to a deal.",
}

# The same reasons in a few words, with no numbers (the turn's title carries
# the number), for the collapsed rows of the trace list. Same keys as REASON_TEXT.
REASON_SHORT: dict[str, str] = {
    "opening": "Required introduction, then ask for their terms.",
    "ask_field": "We need this term before we can check any plan.",
    "ask_settlement": "All the payment rules are known, so price comes next.",
    "read_back": "Making sure we heard an unclear term right.",
    "clarify_field": "The rep gave two different values.",
    "tiers_ambiguous": "Their minimum payment rules were unclear.",
    "cents_ambiguity": "Dollars or cents? We ask before using it.",
    "amount_meaning": "Total or per payment? We ask before using it.",
    "counter": "A step toward their ask that the client can afford.",
    "counter_no_total": "A step toward their ask that the client can afford.",
    "confirm": "The client can afford it, so we ask for a yes.",
    "ask_within_offer": "Their ask is no more than our own offer.",
    "rep_firm": "The rep will not go lower, and the client can afford it.",
    "counters_exhausted": "No counteroffers left, and their ask is affordable.",
    "no_lower_counter": "No lower offer would be affordable.",
    "ladder_stalled": "Another counteroffer would not help.",
    "gap_small": "Too close to their ask to counter again.",
    "terms_revised": "The rules changed since we last confirmed.",
    "alt_first_payment_date": "Their start date leaves no affordable plan.",
    "alt_min_payment_cents": "Their minimum payment blocks every affordable plan.",
    "alt_max_payments": "More payments make a better offer affordable.",
    "confirmed": "The rep agreed; the client approves next.",
    "thanks_accept": "Deal agreed, so we thank them and close.",
    "post_wrap": "The proposal is already with the client.",
    "already_ended": "The call has already ended.",
    "schedule_detail": "The rep asked for the details.",
    "schedule_detail_post_wrap": "The rep asked to hear the schedule.",
    "wrap_renegotiate": "The rep changed the terms after the proposal.",
    "rep_ended": "The rep ended the conversation.",
    "rep_ended_after_wrap": "The rep ended the call after the proposal.",
    "private_info": "They asked about the client's private finances.",
    "sensitive_request": "They kept asking for private information.",
    "commitment": "Only the client can approve a settlement.",
    "commitment_demand": "They kept demanding a binding promise.",
    "hostile": "The conversation became too hostile.",
    "contradiction_unresolved": "Conflicting terms stayed unresolved.",
    "tiers_unresolved": "The minimum payment rules stayed unclear.",
    "amount_meaning_unresolved": "What their dollar amount covers stayed unclear.",
    "already_escalated": "Waiting for a specialist to join.",
    "out_of_guardrail": "Only extra money from the client would make a plan work.",
    "infeasible": "No affordable plan fits their rules.",
    "no_legal_counter": "Nothing below their ask is affordable.",
    "max_counters": "They turned down our best affordable offer.",
    "confirm_unacked": "The rep never agreed to the schedule.",
    "confirm_rejected": "The rep rejected the schedule.",
    "max_turns": "The call ran too long without a deal.",
    "wants_to_end": "The rep wants to stop.",
}

# Intent-level fallback when a code is unknown (new code without an entry).
_GENERIC = "Our code chose this move ({intent})."
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


def reason_short(intent: Intent | str, reason: str | None) -> str | None:
    """The few-word, number-free form of a reason (``None`` for an unknown code)."""
    return REASON_SHORT.get(reason_key(intent, reason))


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
