"""Short NLU / NLG prompt builders for the role-based LLM client.

NLU: field units + last agent line + rep utterance → JSON only.
NLG: intent instruction + placeholder meanings (never values or digits).
Callers: ``app.agent.nlu`` and ``app.agent.nlg``. Does not call the LLM.
"""

from __future__ import annotations

from app.domain.actions import Intent

# Meanings only — never interpolate spoken values or digits here.
PLACEHOLDER_MEANINGS: dict[str, str] = {
    "firm_name": "our firm name",
    "opening_disclosure": "synthetic-data disclosure sentence",
    "ask_text": "question asking for a missing creditor term",
    "readback_value": "the tentative term value being confirmed",
    "field_label": "human label for the creditor term under discussion",
    "clarify_old": "earlier value the rep gave",
    "clarify_new": "newer conflicting value the rep gave",
    "counter_pct": "settlement percentage we are proposing",
    "offer_total": "dollar total of the proposed settlement",
    "num_payments": "number of payments in the schedule",
    "first_payment_date": "date of the first payment",
    "alt_first_payment_date": "alternate earlier first payment date we propose",
    "no_deal_reason": "brief reason we cannot settle",
    "escalate_reason": "brief reason we need a specialist",
}

_INTENT_INSTRUCTION: dict[Intent, str] = {
    Intent.OPENING: "Greet and invite the rep to state their request.",
    Intent.ASK: "Ask for the missing creditor term using {ask_text}.",
    Intent.ASK_SETTLEMENT: "Ask what settlement percentage of the balance they want.",
    Intent.READ_BACK: (
        "Confirm the tentative {field_label} using {readback_value}."
    ),
    Intent.CLARIFY: "Ask which of {clarify_old} or {clarify_new} is correct.",
    Intent.REFUSE_PRIVATE: "Refuse to share client private financials.",
    Intent.REFUSE_COMMIT: "Refuse to commit; say we can only propose to the client.",
    Intent.COUNTER: "Propose {counter_pct} of the balance equaling {offer_total}.",
    Intent.COUNTER_TERMS: (
        "Explain the requested start date does not fit, and propose "
        "{alt_first_payment_date} instead."
    ),
    Intent.CONFIRM_SCHEDULE: (
        "Confirm a schedule with {num_payments} payments totaling {offer_total}, "
        "starting {first_payment_date}."
    ),
    Intent.PROPOSE_WRAP: "Say you will take the proposal to the client for approval.",
    Intent.NO_DEAL_WRAP: "Politely end using {no_deal_reason}.",
    Intent.ESCALATE: "Say a specialist must join. Include {escalate_reason}.",
}

_NLU_SYSTEM = """\
Extract creditor settlement terms from the collection rep's utterance.
Reply with JSON only in this exact shape (no other top-level keys):
{
  "terms":[{"field":"max_payments","value":6,"quote":"six payments","hedged":false}],
  "settlement_ask_pct":45.0,
  "ask_quote":"forty-five percent",
  "stance":"info",
  "readback_response":null,
  "asks_client_private_info":false,
  "demands_commitment":false,
  "hostility":0.0,
  "wants_to_end":false
}
Field names for terms[].field (only these):
max_payments, min_payment_cents, payment_structure, first_payment_date,
max_segments, max_token_pays, min_payment_tiers.
Units:
- max_payments / max_segments / max_token_pays: integers
- min_payment_cents: dollars as integer cents ($250 → 25000)
- payment_structure: "even" | "balloon" | "flexible"
- first_payment_date: YYYY-MM-DD
- min_payment_tiers: list of {"up_to_payments":int,"min_cents":int}
- settlement_ask_pct: percent points as a bare number (45.0), not an object
Each term quote must be a verbatim substring of the utterance.
hedged=true for hedges like about/around/roughly.
If pending_readback is set, fill readback_response with "confirm" or "deny".
stance must be one of: offer, counter, accept, reject, stall, info, question, other.
Use accept when the rep agrees to a schedule or counter ("agreed", "that works").
Use reject when they refuse terms or say a schedule does not work.
Omit unknown fields; use [] / null when nothing extracted.
"""


def nlu_messages(
    utterance: str,
    last_agent_line: str,
    pending_readback: str | None,
) -> list[dict[str, str]]:
    """Build chat messages for one NLU turn (JSON-only reply)."""
    pending = pending_readback or "(none)"
    user = (
        f"Agent last said: {last_agent_line or '(opening)'}\n"
        f"Pending read-back field: {pending}\n"
        f"Rep utterance: {utterance}\n"
        "Reply with JSON only."
    )
    return [
        {"role": "system", "content": _NLU_SYSTEM},
        {"role": "user", "content": user},
    ]


def nlg_messages(
    intent: Intent,
    placeholder_ids: list[str],
    last_rep_line: str,
) -> list[dict[str, str]]:
    """Build chat messages for one NLG template (placeholders, no values)."""
    instruction = _INTENT_INSTRUCTION.get(intent, "Respond briefly and politely.")
    lines = []
    for pid in placeholder_ids:
        meaning = PLACEHOLDER_MEANINGS.get(pid, "a spoken fact placeholder")
        lines.append(f"- {{{pid}}}: {meaning}")
    ph = "\n".join(lines) if lines else "(no placeholders)"
    system = (
        "You write short spoken templates for a debt-settlement agent. "
        "Polite, 1–2 sentences, under 25 words. "
        "Never write digits, number words, $, or %. "
        "Numbers only via the listed {placeholders}. "
        "Never say agree, commit, or deal. "
        "Output only the template text."
    )
    user = (
        f"Rep last said: {last_rep_line or '(none)'}\n"
        f"Intent: {intent.value}\n"
        f"Instruction: {instruction}\n"
        f"Placeholders:\n{ph}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
