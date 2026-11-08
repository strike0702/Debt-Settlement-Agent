"""Short NLU / NLG prompt builders for the role-based LLM client.

NLU (``nlu_acts_messages``): last agent line + rep utterance + a PUBLIC
call-context JSON block (``app.agent.nlu.context``) → a typed act list, JSON
only.
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
    "ack_total": "dollar settlement total the rep just asked for",
    "amount_in_question": "dollar amount the rep said whose meaning we are checking",
}

# How many recent public turns the NLG prompt sees (A/B pre-registration, H3).
NLG_CONTEXT_TURNS = 3

_INTENT_INSTRUCTION: dict[Intent, str] = {
    Intent.OPENING: "Introduce yourself as the caller and ask the rep for settlement terms.",
    Intent.ASK: (
        "Output exactly {ask_text} and nothing else. "
        "Never mention internal field names."
    ),
    Intent.ASK_SETTLEMENT: "Ask what settlement percentage of the balance they want.",
    Intent.READ_BACK: (
        "Ask the rep to confirm {readback_value} for the {field_label}."
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

# NLU: typed acts plus a PUBLIC call context block.
_NLU_ACTS_SYSTEM = """\
You read one line a creditor's rep said on a debt-settlement call and list the acts in it.
Reply with one JSON object only: {"acts":[...]}. Include every act the line contains, in order,
each with only its own keys and "quote": the shortest verbatim span that holds it.
A statement with no rule, price or request (the balance, small talk, "take it to the
client", "no rush") is {"acts":[]}. Only rules the rep states: never infer a rule
from other words. Hedged, unsure or disfluent lines still map ("I think the minimum is
about $200" -> state_rule, hedged). Use "unclear" (alone) only for unintelligible speech;
a long, pushy or mixed line still lists its acts. At most 6 acts, each said once.
Context (JSON) says what the agent is waiting for ("awaiting"), the offers on the table
and any list being collected. Read short or bare replies against it. In known_rules,
source "operator" means the agent stated that rule from its records: a rep value that
differs ("no, it's six now") is a state_rule for that field; a bare "yes / that's right"
after the agent mentioned it confirms it (no act needed beyond backchannel).
Fields: max_payments, max_segments, max_token_pays (integers); min_payment_cents
(integer cents, per payment: "$190" -> 19000); payment_structure (even|balloon|flexible);
first_payment_date (YYYY-MM-DD); min_payment_tiers.
Act types, one example each:
answer: reply to the field the agent asked.
 {"type":"answer","field":"max_payments","value":6,"quote":"six payments"}
state_rule: a rule nobody asked about. Add "hedged":true for about/around.
 {"type":"state_rule","field":"payment_structure","value":"even","quote":"even payments"}
announce_rules: will give or go over a rule but has not yet; count = how many tiers.
 {"type":"announce_rules","field":"min_payment_tiers","quote":"I need to define the payment tiers"}
 "there are three tiers" -> count 3. A number of payment levels is the rule itself:
 "at most two payment levels" -> state_rule max_segments 2. "No tiers" -> state_rule
 min_payment_tiers with value [].
rule_item: one tier: payments it covers and its minimum.
 {"type":"rule_item","from_payment":3,"to_payment":5,"min_cents":25000,
  "quote":"payments three to five must be at least $250"}
list_done: the list is finished.
 {"type":"list_done","quote":"that's all the tiers"}
price_ask: settlement price as pct of the balance or total_cents; firmness opening|counter|final
(final = their floor, lowest, cannot go lower). When the rep corrects themselves ("thirty...
no, sorry, seventy five percent") only the corrected number is the ask.
 {"type":"price_ask","pct":73,"firmness":"final","quote":"seventy three is the lowest I can go"}
 {"type":"price_ask","total_cents":42000,"firmness":"counter","quote":"pay $420 by March 31"}
respond_to_offer: accept or reject what the agent offered ("not sure" is a stall).
 {"type":"respond_to_offer","response":"accept","quote":"that works"}
readback_reply: yes/no to the agent's read-back; correction = the right value if given.
 {"type":"readback_reply","response":"deny","correction":7,"quote":"no, seven"}
question: a question or request for information ("send me the dates"); topic
why_not_higher|next_steps|who_approves|timeline|other.
 {"type":"question","topic":"next_steps","quote":"what happens next?"}
ask_private_info: asks, directly or not, about the client's own money: income, take-home
pay, savings, bank or program balance, assets, budget, what the client can afford, or how
much the client pays, deposits or has drafted each month, what they have left after rent or
bills, disposable or spare money. Not the debt itself.
 {"type":"ask_private_info","quote":"how much does the client make?"}
demand_commitment: demands the agent commit or guarantee now.
 {"type":"demand_commitment","quote":"I need your commitment today"}
hostility: insults or threats only (a firm instruction is not); level mild|strong.
 {"type":"hostility","level":"strong","quote":"you people are crooks"}
end_call: kind goodbye|refuse|transfer (not "that's all" closing a list).
 {"type":"end_call","kind":"goodbye","quote":"thanks, bye"}
stall: puts off their own answer. Not: "take it to your client", "I'll wait for the
client", "no rush" (no act).
 {"type":"stall","quote":"let me check"}
backchannel: filler with no content.
 {"type":"backchannel","quote":"mm-hmm"}
repeat_request: asks the agent to say again or recall what it said (not garbled speech, not
new information).
 {"type":"repeat_request","quote":"can you come again?"}
unclear:
 {"type":"unclear","quote":""}
"""


def nlu_acts_messages(
    utterance: str,
    last_agent_line: str,
    context_json: str,
    *,
    ref: date | None = None,
) -> list[dict[str, str]]:
    """Chat messages for one acts-schema NLU turn (JSON-only reply).

    ``context_json`` is ``CallContext.prompt_json()``: PUBLIC call state only.
    """
    today = f"Today: {ref.isoformat()}\n" if ref is not None else ""
    user = (
        today
        + f"Context: {context_json}\n"
        f"Agent last said: {last_agent_line or '(opening)'}\n"
        f"Rep: {utterance}"
    )
    return [
        {"role": "system", "content": _NLU_ACTS_SYSTEM},
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
