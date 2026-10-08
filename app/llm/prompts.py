"""Short NLU / NLG prompt builders for the role-based LLM client.

NLU: field units + last agent line + rep utterance → JSON only.
NLG: intent instruction + placeholder meanings (never values or digits), plus
up to the last 3 public turns as context (spoken lines only; the caller drops
any line that carries a private figure). ``act_messages`` builds the offline
bank prompt for the H3 ack / answer acts.
Callers: ``app.agent.nlu`` and ``app.agent.nlg``. Does not call the LLM.
"""

from __future__ import annotations

from datetime import date

from app.domain.actions import Intent

# Meanings only — never interpolate spoken values or digits here.
PLACEHOLDER_MEANINGS: dict[str, str] = {
    "firm_name": "our firm name",
    "opening_disclosure": "authorization / agent-role sentence",
    "ask_text": "question asking for a missing creditor term",
    "readback_value": "the tentative term value being confirmed",
    "field_label": "human label for the creditor term under discussion",
    "clarify_old": "earlier value the rep gave",
    "clarify_new": "newer conflicting value the rep gave",
    "counter_pct": "settlement percentage we are proposing",
    "settlement_pct": "settlement percentage both sides are aligning on",
    "offer_total": "dollar total of the proposed settlement",
    "num_payments": "number of payments in the schedule",
    "first_payment_date": "date of the first payment",
    "alt_first_payment_date": "alternate first payment date we propose",
    "alt_min_payment_cents": "lower minimum payment we propose",
    "alt_max_payments": "higher maximum payment count we propose",
    "no_deal_reason": "brief reason we cannot settle",
    "escalate_reason": "brief reason we need a specialist",
    "answer_text": "policy talking point answering the rep's question",
    "ack_max_payments": "maximum number of payments the rep just stated",
    "ack_min_payment": "minimum payment amount the rep just stated",
    "ack_first_payment_date": "first payment date the rep just stated",
    "amount_in_question": "dollar amount the rep said whose meaning we are checking",
}

# How many recent public turns the NLG prompt sees (REVIEW_PLAN §2(c), H3).
NLG_CONTEXT_TURNS = 3

_INTENT_INSTRUCTION: dict[Intent, str] = {
    Intent.OPENING: "Introduce yourself as the caller and ask the rep for settlement terms.",
    Intent.ASK: (
        "Output exactly {ask_text} and nothing else. "
        "Never mention internal field names."
    ),
    Intent.ASK_SETTLEMENT: "Ask what settlement percentage of the balance they want.",
    Intent.READ_BACK: (
        "Confirm the tentative {field_label} using {readback_value}."
    ),
    Intent.CLARIFY: "Ask which of {clarify_old} or {clarify_new} is correct.",
    Intent.REFUSE_PRIVATE: "Refuse to share client private financials.",
    Intent.REFUSE_COMMIT: "Refuse to commit; say we can only propose to the client.",
    Intent.COUNTER: "Propose {counter_pct} of the balance equaling {offer_total}.",
    Intent.COUNTER_TERMS: (
        "Propose a non-price adjustment using whichever alt placeholder is present: "
        "{alt_first_payment_date}, {alt_min_payment_cents}, or {alt_max_payments}."
    ),
    Intent.CONFIRM_SCHEDULE: (
        "First acknowledge {settlement_pct} works, then propose (do not confirm) "
        "a schedule with {num_payments} payments totaling {offer_total}, "
        "starting {first_payment_date}, and ask if it works. "
        "Never say deal, agree, or commit."
    ),
    Intent.SPEAK_SCHEDULE: (
        "Read the payment schedule date by date using only the given placeholders."
    ),
    Intent.PROPOSE_WRAP: (
        "Say the proposal was sent to the client for approval, then ask if they "
        "need anything else before ending the call. Never say deal, agree, or commit."
    ),
    Intent.CLOSE: (
        "Thank the rep and say you will follow up after the client reviews. End the call."
    ),
    Intent.NO_DEAL_WRAP: "Politely end using {no_deal_reason}.",
    Intent.ESCALATE: "Say a specialist must join. Include {escalate_reason}.",
    Intent.ANSWER: "Output exactly {answer_text} and nothing else.",
}

_NLU_SYSTEM = """\
Extract creditor settlement terms from the collection rep's utterance.
Reply with JSON only in this exact shape (no other top-level keys):
{
  "terms":[{"field":"max_payments","value":6,"quote":"six payments","hedged":false}],
  "settlement_ask_pct":45.0,
  "ask_quote":"forty-five percent",
  "settlement_ask_total_cents":null,
  "ask_total_quote":null,
  "amount_ambiguous_cents":null,
  "amount_ambiguous_quote":null,
  "stance":"info",
  "readback_response":null,
  "asks_client_private_info":false,
  "demands_commitment":false,
  "hostility":0.0,
  "wants_to_end":false,
  "asks_for_schedule":false,
  "firm":false,
  "asks_question":false,
  "question_topic":null
}
Field names for terms[].field (only these):
max_payments, min_payment_cents, payment_structure, first_payment_date,
max_segments, max_token_pays, min_payment_tiers.
Units:
- max_payments / max_segments / max_token_pays: integers
- min_payment_cents: dollars as integer cents ($250 → 25000), only for the smallest
  amount of each payment (per payment, each, a month, monthly, minimum, at least
  $X a payment).
  Bare digits with no $ / dollars / cents stay empty so the agent can clarify.
- payment_structure: "even" | "balloon" | "flexible"
- first_payment_date: YYYY-MM-DD
- min_payment_tiers: list of {"from_payment":int,"min_cents":int}; from_payment is
  the 1-based payment number where that higher minimum starts
  ("from the fourth payment on, at least $75" → [{"from_payment":4,"min_cents":7500}]).
  "No tiers" / "no tiered minimums" → [].
- settlement_ask_pct: percent points as a bare number (45.0), not an object
- settlement_ask_total_cents: the settlement ask as a dollar TOTAL in integer cents
  ("settle for $1,200 total" → 120000), with ask_total_quote. Cues: total, in full,
  to settle, settle for, pay $X by a date with no payment count or per-payment word.
- amount_ambiguous_cents: a dollar amount (integer cents) that could be either the
  total to settle or the minimum per payment, with amount_ambiguous_quote. Use it
  instead of guessing, e.g. "$350, three payments" or "pay $420 by March 31, we only
  accept 3 payments". Do not also put that amount in terms or the total.
Each term quote, ask_total_quote and amount_ambiguous_quote must be a verbatim
substring of the utterance.
hedged=true for hedges like about/around/roughly.
If pending_readback is set, fill readback_response with "confirm" or "deny".
stance must be one of: offer, counter, accept, reject, stall, info, question, other.
- info: the rep states a fact, rule or limit (payment count, minimum per payment,
  even/balloon, first payment date, balance, policy), even hedged, and names no
  settlement price. Extracting a term does not make a line an offer or counter.
  Stance never changes extraction: still put every rule or limit the line states
  in terms, and a dollar amount with no total or per-payment cue stays ambiguous.
  Telling the agent to take it to the client, or that there is no rush, is info.
- counter: the rep names a settlement price (a percent of the balance or a dollar
  total to settle), or asks to change a price or term already proposed by either side.
  offer: the same, but only when Agent last said is (opening).
- accept when the rep agrees to a schedule or counter ("agreed", "that works").
- reject when they refuse terms, are unhappy with an offer, or say a schedule
  does not work.
- question: the rep asks a question or requests information. Demands are not questions.
- stall: the rep puts off their own answer (hold on, let me check, I'll ask my
  supervisor, I'm not sure).
- other: closings, goodbyes, impatience to end the call, insults, and fillers or
  unfinished fragments with no content.
  A rep who is ending or wants to end the call is other, never stall.
Examples: "Six installments is our limit." → info; "We'd take fifty-two percent." →
counter; "Give me a minute to check." → stall; "Okay, I'll let you go." → other.
Set wants_to_end=true for thanks, thank you, goodbye, bye, that's all, or similar closings.
asks_client_private_info=true when the rep asks about the client's own money, directly
or indirectly: income, take-home pay, savings, bank or program account balance, assets,
budget, what the client can afford, or how much the client pays, deposits or has
drafted each month ("How much is the client saving toward this each week?").
Not for questions about the debt itself: balance owed, account number, payment
history, or the terms being proposed.
Set asks_for_schedule=true when the rep asks for payment dates or amounts per payment.
firm=true only when the rep says the number is final, their floor, or they cannot go lower.
asks_question=true only for an off-script process question the terms do not answer:
why the offer is not higher, next steps, who approves, or how long things take.
Not for questions about terms, percentages, schedules, or the client's finances.
question_topic: why_not_higher | next_steps | who_approves | timeline | other
(null when asks_question is false).
Omit unknown fields; use [] / null when nothing extracted.
"""


def nlu_messages(
    utterance: str,
    last_agent_line: str,
    pending_readback: str | None,
    *,
    ref: date | None = None,
) -> list[dict[str, str]]:
    """Build chat messages for one NLU turn (JSON-only reply).

    ``ref`` is the call's reference date so a yearless "May 15" can resolve.
    """
    pending = pending_readback or "(none)"
    today = f"Today's date: {ref.isoformat()}\n" if ref is not None else ""
    user = (
        today
        + f"Agent last said: {last_agent_line or '(opening)'}\n"
        f"Pending read-back field: {pending}\n"
        f"Rep utterance: {utterance}\n"
        "Reply with JSON only."
    )
    return [
        {"role": "system", "content": _NLU_SYSTEM},
        {"role": "user", "content": user},
    ]


def format_recent_turns(recent_turns: list[tuple[str, str]]) -> str:
    """``Rep: …`` / ``Agent: …`` lines for the last ``NLG_CONTEXT_TURNS`` turns."""
    names = {"creditor": "Rep", "agent": "Agent"}
    tail = recent_turns[-NLG_CONTEXT_TURNS:]
    return "\n".join(f"{names.get(role, role)}: {text}" for role, text in tail)


def nlg_messages(
    intent: Intent,
    placeholder_ids: list[str],
    last_rep_line: str,
    *,
    recent_turns: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build chat messages for one NLG template (placeholders, no values).

    With ``recent_turns`` the prompt carries the last few public turns
    (oldest first) instead of only the rep's last line.
    """
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
    if recent_turns:
        context = f"Recent conversation:\n{format_recent_turns(recent_turns)}\n"
    else:
        context = f"Rep last said: {last_rep_line or '(none)'}\n"
    user = (
        context
        + f"Intent: {intent.value}\n"
        f"Instruction: {instruction}\n"
        f"Placeholders:\n{ph}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def act_messages(
    kind: str,
    placeholder_ids: list[str],
    *,
    talking_point: str | None = None,
) -> list[dict[str, str]]:
    """Offline bank prompt for an H3 act: ``ack`` (placeholders) or ``answer`` (paraphrase).

    Used only by ``scripts/build_template_bank.py``; the talking point is
    policy text and number-free.
    """
    system = (
        "You write short spoken lines for a debt-settlement agent. "
        "Never write digits, number words, $, or %. "
        "Never say agree, commit, or deal. Output only the line."
    )
    if kind == "ack":
        lines = "\n".join(
            f"- {{{pid}}}: {PLACEHOLDER_MEANINGS.get(pid, 'a spoken fact')}"
            for pid in placeholder_ids
        )
        user = (
            "Write one short acknowledgement sentence (under 15 words) that echoes "
            "the terms the rep just gave, using every placeholder exactly once, "
            "before the agent's next question. Example: "
            "Got it, {ack_max_payments} payments at a {ack_min_payment} minimum.\n"
            f"Placeholders:\n{lines}"
        )
    else:
        user = (
            "Rephrase this answer to the rep's question in one or two short sentences "
            "with the same meaning. Add no new facts or promises.\n"
            f"Answer: {talking_point}"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
