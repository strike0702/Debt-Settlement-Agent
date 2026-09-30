"""Deterministic NLG templates and render pipeline (no LLM in this phase).

Each ``Intent`` has one template with ``{placeholders}``. ``render_action`` runs
``template_guard``, fills ``text_slots`` then PUBLIC ``Fact.render``, then
``rendered_guard``. Any guard failure yields ``SAFE_FALLBACK``.
"""

from __future__ import annotations

from datetime import date

from app.agent.guards import rendered_guard, template_guard
from app.agent.policy import Action, Intent

SAFE_FALLBACK = "Let me check that figure and come back to it."

# One spoken template per intent. No digits, $, %, or number-words.
TEMPLATES: dict[Intent, str] = {
    Intent.OPENING: (
        "Hello, this is {firm_name}. {opening_disclosure} "
        "How can I help with this account today?"
    ),
    Intent.ASK: "{ask_text}",
    Intent.ASK_SETTLEMENT: (
        "What settlement percentage of the balance are you looking for?"
    ),
    Intent.READ_BACK: "So I have {readback_value} for that term. Is that right?",
    Intent.CLARIFY: (
        "Earlier you mentioned {clarify_old}, and now I am hearing {clarify_new}. "
        "Which of those should I use?"
    ),
    Intent.REFUSE_PRIVATE: (
        "I cannot share the client's private financial information."
    ),
    Intent.REFUSE_COMMIT: (
        "I can propose this to the client, but I cannot commit on the call."
    ),
    Intent.COUNTER: (
        "We can propose {counter_pct} of the balance, which is {offer_total}. "
        "Would that work?"
    ),
    Intent.CONFIRM_SCHEDULE: (
        "We can do {num_payments} payments totaling {offer_total}, "
        "starting {first_payment_date}."
    ),
    Intent.PROPOSE_WRAP: (
        "I can take this proposal to the client for approval."
    ),
    Intent.NO_DEAL_WRAP: (
        "I do not think we can make a settlement work under these terms. "
        "Thank you for your time."
    ),
    Intent.ESCALATE: (
        "I need to involve someone from our side. {escalate_reason}"
    ),
}


def _allowed_ids(action: Action) -> set[str]:
    return set(action.facts.keys()) | set(action.text_slots.keys())


def _fill_template(template: str, action: Action, ref: date) -> str:
    filled = template
    for key, value in action.text_slots.items():
        filled = filled.replace("{" + key + "}", value)
    for key, fact in action.facts.items():
        filled = filled.replace("{" + key + "}", fact.render(ref))
    return filled


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("? ", "?|").replace(". ", ".|").split("|")]
    return [p for p in parts if p]


def render_action(
    action: Action,
    ref_date: date,
    *,
    creditor_numbers: set[tuple[str, int | date]] | None = None,
    private_blocklist: set[tuple[str, int | date]] | None = None,
) -> list[str]:
    """Render an action to spoken sentences, or ``SAFE_FALLBACK`` on guard fail."""
    template = TEMPLATES[action.intent]
    allowed = _allowed_ids(action)
    # Placeholders present in the template that we expect to fill.
    tg = template_guard(template, allowed, action.required)
    if not tg.ok:
        return [SAFE_FALLBACK]

    spoken = _fill_template(template, action, ref_date)
    # Leftover unfilled placeholders → fail closed.
    if "{" in spoken and "}" in spoken:
        return [SAFE_FALLBACK]

    sentences = _split_sentences(spoken)
    creditor = creditor_numbers or set()
    private = private_blocklist or set()
    public = action.facts

    out: list[str] = []
    for sentence in sentences:
        rg = rendered_guard(
            sentence,
            public,
            creditor,
            private,
            ref=ref_date,
        )
        if not rg.ok:
            return [SAFE_FALLBACK]
        out.append(sentence)
    return out if out else [SAFE_FALLBACK]
