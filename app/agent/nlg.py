"""Deterministic NLG templates plus optional LLM template generation.

Deterministic ``TEMPLATES`` / sync ``render_action`` are the fallback and the
``NLG_MODE=template`` path. ``speak_action`` (async) may ask the LLM for a
template, run ``template_guard``, retry once, then fall back; fill facts; run
``rendered_guard``. LLM never receives PRIVATE values or digits — only
placeholder meanings from ``app.llm.prompts``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from app.agent.guards import rendered_guard, template_guard
from app.config import Settings, get_settings
from app.domain.actions import Action, Intent
from app.llm.prompts import nlg_messages
from app.store.audit import AuditLog

SAFE_FALLBACK = "Let me check that figure and come back to it."

# These intents' slots are already full sentences (``ask_text``, ``no_deal_reason``,
# ``escalate_reason``). LLM rewrites wrap them in another sentence, producing
# mid-sentence capitals and leaked field names, so they always use ``TEMPLATES``.
TEMPLATE_ONLY_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.ASK,
        Intent.ASK_SETTLEMENT,
        Intent.SPEAK_SCHEDULE,
        Intent.CLOSE,
        Intent.NO_DEAL_WRAP,
        Intent.ESCALATE,
    }
)

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
    Intent.READ_BACK: (
        "So I have {readback_value} for the {field_label}. Is that right?"
    ),
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
    Intent.COUNTER_TERMS: (
        "That start date does not fit the client's program. "
        "Could payment start on {alt_first_payment_date} instead?"
    ),
    Intent.CONFIRM_SCHEDULE: (
        "We can do {num_payments} payments totaling {offer_total}, "
        "starting {first_payment_date}."
    ),
    Intent.SPEAK_SCHEDULE: (
        "Here is the payment-by-payment schedule."
    ),
    Intent.PROPOSE_WRAP: (
        "I can take this proposal to the client for approval."
    ),
    Intent.CLOSE: (
        "Thank you. We will present this to the client and follow up "
        "after their review. Have a good day."
    ),
    Intent.NO_DEAL_WRAP: (
        "{no_deal_reason} Thank you for your time."
    ),
    Intent.ESCALATE: (
        "I need to involve someone from our side. {escalate_reason}"
    ),
}


class _TextLLM(Protocol):
    async def chat_text(
        self,
        role: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str: ...


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


def _note_blocked(
    entry: dict[str, Any],
    *,
    audit: AuditLog | None,
    call_id: str | None,
    blocked_out: list[dict[str, Any]] | None,
) -> None:
    """Append a guard-block record to audit and/or the caller's sink."""
    if blocked_out is not None:
        blocked_out.append(entry)
    if audit is not None and call_id is not None:
        audit.append(call_id, "nlg", "blocked", entry)


def _render_filled(
    template: str,
    action: Action,
    ref_date: date,
    *,
    creditor_numbers: set[tuple[str, int | date]] | None,
    private_blocklist: set[tuple[str, int | date]] | None,
    audit: AuditLog | None,
    call_id: str | None,
    blocked_out: list[dict[str, Any]] | None = None,
) -> list[str]:
    allowed = _allowed_ids(action)
    tg = template_guard(template, allowed, action.required)
    if not tg.ok:
        _note_blocked(
            {"stage": "template", "reason": tg.reason, "offending": tg.offending},
            audit=audit,
            call_id=call_id,
            blocked_out=blocked_out,
        )
        return [SAFE_FALLBACK]

    spoken = _fill_template(template, action, ref_date)
    if "{" in spoken and "}" in spoken:
        _note_blocked(
            {"stage": "unfilled", "reason": "unfilled_placeholder"},
            audit=audit,
            call_id=call_id,
            blocked_out=blocked_out,
        )
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
            _note_blocked(
                {
                    "stage": "rendered",
                    "reason": rg.reason,
                    "offending": rg.offending,
                },
                audit=audit,
                call_id=call_id,
                blocked_out=blocked_out,
            )
            return [SAFE_FALLBACK]
        out.append(sentence)
    return out if out else [SAFE_FALLBACK]


def render_action(
    action: Action,
    ref_date: date,
    *,
    creditor_numbers: set[tuple[str, int | date]] | None = None,
    private_blocklist: set[tuple[str, int | date]] | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
    blocked_out: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Render the deterministic template, or ``SAFE_FALLBACK`` on guard fail."""
    template = action.template_override or TEMPLATES[action.intent]
    return _render_filled(
        template,
        action,
        ref_date,
        creditor_numbers=creditor_numbers,
        private_blocklist=private_blocklist,
        audit=audit,
        call_id=call_id,
        blocked_out=blocked_out,
    )


async def speak_action(
    action: Action,
    ref_date: date,
    *,
    llm: _TextLLM | None = None,
    settings: Settings | None = None,
    last_rep_line: str = "",
    creditor_numbers: set[tuple[str, int | date]] | None = None,
    private_blocklist: set[tuple[str, int | date]] | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
    blocked_out: list[dict[str, Any]] | None = None,
) -> list[str]:
    """LLM template (optional) → template_guard → fill → rendered_guard.

    On LLM/template failure after one retry, falls back to ``TEMPLATES``.
    ``TEMPLATE_ONLY_INTENTS`` always use ``TEMPLATES`` (no LLM call).
    """
    cfg = settings or get_settings()
    allowed = _allowed_ids(action)
    required = action.required
    template = action.template_override or TEMPLATES[action.intent]

    use_llm = (
        action.intent not in TEMPLATE_ONLY_INTENTS and action.template_override is None
    )
    if use_llm and cfg.nlg_mode == "llm" and llm is not None:
        placeholder_ids = sorted(allowed)
        messages = nlg_messages(action.intent, placeholder_ids, last_rep_line)
        candidate: str | None = None
        for attempt in range(2):
            try:
                msgs = list(messages)
                if attempt == 1 and candidate is not None:
                    msgs = [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                "Previous template failed guards (digits, $, %, "
                                "number words, or bad placeholders). "
                                "Rewrite using only the listed placeholders."
                            ),
                        },
                    ]
                text = (await llm.chat_text("nlg", msgs, 400)).strip()

                # Strip accidental fences
                if text.startswith("```"):
                    lines = text.split("\n")
                    text = "\n".join(
                        ln for ln in lines if not ln.strip().startswith("```")
                    ).strip()
                tg = template_guard(text, allowed, required)
                if tg.ok:
                    candidate = text
                    break
                candidate = text  # keep for retry hint
                if audit is not None and call_id is not None:
                    audit.append(
                        call_id,
                        "nlg",
                        "nlg_template_rejected",
                        {
                            "attempt": attempt,
                            "reason": tg.reason,
                            "offending": tg.offending,
                        },
                    )
            except Exception as e:
                if audit is not None and call_id is not None:
                    audit.append(
                        call_id,
                        "nlg",
                        "nlg_llm_failed",
                        {"attempt": attempt, "error": str(e)},
                    )
                candidate = None
        if candidate is not None:
            tg = template_guard(candidate, allowed, required)
            if tg.ok:
                template = candidate

    return _render_filled(
        template,
        action,
        ref_date,
        creditor_numbers=creditor_numbers,
        private_blocklist=private_blocklist,
        audit=audit,
        call_id=call_id,
        blocked_out=blocked_out,
    )
