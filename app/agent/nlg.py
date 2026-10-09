"""Deterministic NLG templates plus optional LLM template generation.

Deterministic ``TEMPLATES`` / sync ``render_action`` are the fallback and the
``NLG_MODE=template`` path. ``speak_action`` (async) picks the template by mode:
``bank`` takes a pre-generated, guard-checked template from
``app.agent.nlg_bank`` (no LLM call); ``llm`` asks the LLM, runs
``template_guard``, and retries once. Both fall back to ``TEMPLATES``, then fill
facts and run ``rendered_guard``. LLM never receives PRIVATE values or digits —
only placeholder meanings from ``app.llm.prompts``. An optional ``trace_out``
dict receives how the line was made (mode, template, guard verdicts, fallback)
for the orchestrator's decision trace; it never changes what is spoken.

Phase 24b (H3): ``render_acts`` speaks an action's optional ``ack`` and
``answer`` acts as short leading sentences, each through the same two guards.
Phase 46b: the default agent's code-built ack (``Settings.nlg_ack``) uses
``ack_template`` (2–3 wordings per shape, rotated by turn), never the bank or LLM;
a count of 1 is acked as "up to 1 payment" (Phase 49). Phase 50 acks the
payment structure from a digit-free ``text`` fact ("Got it, a balloon schedule.",
or ", with even payments" after the other terms).
A guard-failed act is dropped (audited), never replaced by ``SAFE_FALLBACK``,
so the primary move is spoken unchanged. ``speak_action`` passes the last few
public turns (``recent_turns``) to the LLM prompt instead of one rep line.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Protocol

from app.agent.guards import GuardResult, rendered_guard, template_guard
from app.agent.nlg_bank import load_bank, pick_template
from app.config import Settings, get_settings
from app.domain.actions import Action, Intent
from app.llm.client import LLMUnavailable
from app.llm.prompts import nlg_messages
from app.store.audit import AuditLog

SAFE_FALLBACK = "Let me check that figure and come back to it."
# gpt-oss reasoning tokens share the completion budget; at 400 the template was
# often empty (carry-over 21.7). The nlg routes also send reasoning_effort: low.
NLG_MAX_TOKENS = 800

# These intents' slots are already full sentences (``ask_text``, ``no_deal_reason``,
# ``escalate_reason``). LLM rewrites wrap them in another sentence, producing
# mid-sentence capitals and leaked field names, so they always use ``TEMPLATES``.
TEMPLATE_ONLY_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.OPENING,
        Intent.ASK,
        Intent.ASK_SETTLEMENT,
        Intent.SPEAK_SCHEDULE,
        Intent.PROPOSE_WRAP,
        Intent.CLOSE,
        Intent.NO_DEAL_WRAP,
        Intent.ESCALATE,
        Intent.ANSWER,
    }
)

# One spoken template per intent. No digits, $, %, or number-words.
TEMPLATES: dict[Intent, str] = {
    Intent.OPENING: (
        "Hello, this is an automated agent calling on behalf of {firm_name} "
        "about a client's account with you. {opening_disclosure} "
        "What payment terms can you work with for a settlement?"
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
        "{settlement_pct} works for us. We can do {num_payments} payments "
        "totaling {offer_total}, starting {first_payment_date}. Would that work?"
    ),
    Intent.SPEAK_SCHEDULE: (
        "Here is the payment-by-payment schedule."
    ),
    Intent.PROPOSE_WRAP: (
        "I have sent this proposal to the client for approval. "
        "Do you need anything else before we end the call?"
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
    # Only spoken as an attached act (``render_acts``); the slot is a talking point.
    Intent.ANSWER: "{answer_text}",
}


# Acknowledgement bodies keyed by the sorted term ``ack_*`` ids present; the
# first body of each is the H3 wording (Phase 24b). An ack line is
# "<opener>, <total body>, <term body>." (the total, Phase 46b, leads).
_ACK_TERM_BODIES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("ack_max_payments",): (
        "up to {ack_max_payments} payments",
        "a maximum of {ack_max_payments} payments",
    ),
    ("ack_min_payment",): (
        "a {ack_min_payment} minimum payment",
        "at least {ack_min_payment} per payment",
    ),
    ("ack_first_payment_date",): (
        "starting {ack_first_payment_date}",
        "with payments beginning {ack_first_payment_date}",
    ),
    ("ack_max_payments", "ack_min_payment"): (
        "{ack_max_payments} payments at a {ack_min_payment} minimum",
        "up to {ack_max_payments} payments, at least {ack_min_payment} each",
    ),
    ("ack_first_payment_date", "ack_max_payments"): (
        "{ack_max_payments} payments starting {ack_first_payment_date}",
        "up to {ack_max_payments} payments, beginning {ack_first_payment_date}",
    ),
    ("ack_first_payment_date", "ack_min_payment"): (
        "a {ack_min_payment} minimum starting {ack_first_payment_date}",
        "at least {ack_min_payment} per payment, starting {ack_first_payment_date}",
    ),
    ("ack_first_payment_date", "ack_max_payments", "ack_min_payment"): (
        "{ack_max_payments} payments at a {ack_min_payment} minimum, "
        "starting {ack_first_payment_date}",
        "up to {ack_max_payments} payments of at least {ack_min_payment}, "
        "starting {ack_first_payment_date}",
    ),
}
_ACK_TOTAL_BODIES: tuple[str, ...] = (
    "{ack_total} in total",
    "{ack_total} for the whole settlement",
)
# Payment structure (Phase 50): the fact renders a phrase ("a balloon schedule",
# "even payments"). Alone it has its own bodies; after other terms it is appended.
_ACK_STRUCTURE_ID = "ack_payment_structure"
_ACK_STRUCTURE_BODIES: tuple[str, ...] = (
    "{ack_payment_structure}",
    "{ack_payment_structure} it is",
)
_ACK_STRUCTURE_TAIL = "with {ack_payment_structure}"


def _ack_bodies(ids: tuple[str, ...]) -> tuple[str, ...] | None:
    """Term bodies (total excluded) for the sorted ids; None when the shape is unknown."""
    terms = tuple(i for i in ids if i not in ("ack_total", _ACK_STRUCTURE_ID))
    if terms and terms not in _ACK_TERM_BODIES:
        return None
    if _ACK_STRUCTURE_ID not in ids:
        return _ACK_TERM_BODIES.get(terms, ("",))
    if not terms:
        # Alone it gets its own bodies; after a dollar total it is the tail.
        return (_ACK_STRUCTURE_TAIL,) if "ack_total" in ids else _ACK_STRUCTURE_BODIES
    return tuple(f"{b}, {_ACK_STRUCTURE_TAIL}" for b in _ACK_TERM_BODIES[terms])
_ACK_OPENERS: tuple[str, ...] = ("Got it", "Understood", "Okay")


def ack_variants(ids: tuple[str, ...]) -> list[str]:
    """Every code-built ack wording for the sorted ``ack_*`` ids (empty if unknown)."""
    found = _ack_bodies(ids)
    if found is None:
        return []
    term_bodies = tuple(b for b in found if b)
    if "ack_total" in ids:
        bodies = [
            f"{tb}, {b}" if b else tb
            for tb in _ACK_TOTAL_BODIES
            for b in (term_bodies or ("",))
        ]
    else:
        bodies = list(term_bodies)
    return [f"{o}, {b}." for o in _ACK_OPENERS for b in bodies]


def ack_template(ids: tuple[str, ...], turn: int, *, singular: bool = False) -> str | None:
    """Deterministic ack wording for this turn; consecutive turns get different lines.

    Opener and body rotate independently so the same shape does not repeat
    word for word on back-to-back turns. ``singular`` (a payment count of 1)
    says "up to 1 payment", not "1 payments" (Phase 49).
    """
    term_bodies = _ack_bodies(ids)
    if term_bodies is None:
        return None
    body = term_bodies[turn % len(term_bodies)]
    if "ack_total" in ids:
        total = _ACK_TOTAL_BODIES[turn % len(_ACK_TOTAL_BODIES)]
        body = f"{total}, {body}" if body else total
    if singular:
        body = body.replace("{ack_max_payments} payments", "{ack_max_payments} payment")
    return f"{_ACK_OPENERS[turn % len(_ACK_OPENERS)]}, {body}."


# H3 (Phase 24b) wording: one fixed line per shape ("Got it, " + first body).
ACK_TEMPLATES: dict[tuple[str, ...], str] = {
    **{ids: f"Got it, {bodies[0]}." for ids, bodies in _ACK_TERM_BODIES.items()},
    (_ACK_STRUCTURE_ID,): f"Got it, {_ACK_STRUCTURE_BODIES[0]}.",
    ("ack_total",): f"Got it, {_ACK_TOTAL_BODIES[0]}.",
}
ANSWER_TEMPLATE = TEMPLATES[Intent.ANSWER]
ACK_BANK_INTENT = "ACK"


def answer_bank_intent(topic: str) -> str:
    """Bank intent key for one question topic's talking-point variants."""
    return f"ANSWER:{topic}"


class _TextLLM(Protocol):
    async def chat_text(
        self,
        role: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str: ...


def _allowed_ids(action: Action) -> set[str]:
    return set(action.facts.keys()) | set(action.text_slots.keys())


def _singular_counts(template: str, action: Action) -> str:
    """"{n} payments" → "{n} payment" for every count fact equal to 1 (Phase 50).

    Bank and default bodies all say "{num_payments} payments totaling ...";
    this keeps a one-payment plan from being spoken as "1 payments".
    """
    for key, fact in action.facts.items():
        if fact.kind == "count" and fact.value == 1:
            template = re.sub(
                r"\{" + re.escape(key) + r"\} (payment|installment)s\b",
                lambda m, k=key: "{" + k + "} " + m.group(1),
                template,
            )
    return template


def _fill_template(template: str, action: Action, ref: date) -> str:
    filled = _singular_counts(template, action)
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


def _trace_guard(
    trace_out: dict[str, Any] | None, stage: str, ok: bool, reason: str = "", offending: Any = None
) -> None:
    if trace_out is None:
        return
    trace_out.setdefault("guards", []).append(
        {
            "stage": stage,
            "ok": ok,
            "reason": reason or None,
            "offending": [str(o) for o in offending] if offending else None,
        }
    )
    if not ok:
        trace_out["fallback_used"] = True
        trace_out["fallback_reason"] = "safe_fallback"


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
    trace_out: dict[str, Any] | None = None,
) -> list[str]:
    if trace_out is not None:
        trace_out["template"] = template
    allowed = _allowed_ids(action)
    tg = template_guard(template, allowed, action.required)
    _trace_guard(trace_out, "template", tg.ok, tg.reason, tg.offending)
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
        _trace_guard(trace_out, "unfilled", False, "unfilled_placeholder")
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
        _trace_guard(trace_out, "rendered", rg.ok, rg.reason, rg.offending)
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
    trace_out: dict[str, Any] | None = None,
) -> list[str]:
    """Render the deterministic template, or ``SAFE_FALLBACK`` on guard fail."""
    template = action.template_override or TEMPLATES[action.intent]
    if trace_out is not None:
        trace_out.setdefault("mode", "template")
        trace_out["source"] = "override" if action.template_override else "default"
    return _render_filled(
        template,
        action,
        ref_date,
        creditor_numbers=creditor_numbers,
        private_blocklist=private_blocklist,
        audit=audit,
        call_id=call_id,
        blocked_out=blocked_out,
        trace_out=trace_out,
    )


def render_acts(
    action: Action,
    ref_date: date,
    *,
    nlg_mode: str = "template",
    bank_path: str | None = None,
    creditor_numbers: set[tuple[str, int | date]] | None = None,
    private_blocklist: set[tuple[str, int | date]] | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
    blocked_out: list[dict[str, Any]] | None = None,
    turn: int = 0,
    code_ack: bool = False,
) -> list[str]:
    """Sentences for ``action.ack`` then ``action.answer`` (empty when neither is set).

    Bank / llm modes take a guard-checked bank variant when one exists (no LLM
    call either way); otherwise ``ACK_TEMPLATES`` / the talking point verbatim.
    ``code_ack`` (the default agent's ``nlg_ack``) always takes the ack from
    ``ack_template`` (code variants by turn), never the bank.
    Each act runs ``template_guard`` + ``rendered_guard``; a failing act is
    dropped and audited as ``act_dropped``.
    """
    use_bank = nlg_mode in ("bank", "llm")
    bank = (load_bank(bank_path) if bank_path else load_bank()) if use_bank else {}
    acts: list[tuple[str, Action, str]] = []
    if action.ack:
        ids = tuple(sorted(action.ack))
        sub = Action(
            intent=action.intent,
            facts=dict(action.ack),
            required=set(ids),
            next_phase=action.next_phase,
        )
        count = action.ack.get("ack_max_payments")
        singular = count is not None and count.value == 1
        if code_ack:
            template = ack_template(ids, turn, singular=singular)
        else:
            template = (
                pick_template(
                    sub, call_id=call_id, turn=turn, bank=bank, intent_key=ACK_BANK_INTENT
                )
                if use_bank and not singular
                else None
            ) or (None if singular else ACK_TEMPLATES.get(ids)) or ack_template(
                ids, 0, singular=singular
            )
        if template is not None:
            acts.append(("ack", sub, template))
    if action.answer is not None:
        bare = Action(intent=Intent.ANSWER, next_phase=action.next_phase)
        picked = (
            pick_template(
                bare,
                call_id=call_id,
                turn=turn,
                bank=bank,
                intent_key=answer_bank_intent(action.answer.topic),
            )
            if use_bank
            else None
        )
        if picked is not None:
            acts.append(("answer", bare, picked))
        else:
            sub = bare.model_copy(update={"text_slots": {"answer_text": action.answer.text}})
            acts.append(("answer", sub, ANSWER_TEMPLATE))

    out: list[str] = []
    for kind, sub, template in acts:
        spoken = _render_filled(
            template,
            sub,
            ref_date,
            creditor_numbers=creditor_numbers,
            private_blocklist=private_blocklist,
            audit=audit,
            call_id=call_id,
            blocked_out=blocked_out,
        )
        if spoken == [SAFE_FALLBACK]:
            if audit is not None and call_id is not None:
                audit.append(call_id, "nlg", "act_dropped", {"act": kind})
            continue
        out.extend(spoken)
    return out


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
    turn: int = 0,
    trace_out: dict[str, Any] | None = None,
    recent_turns: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Bank or LLM template → template_guard → fill → rendered_guard.

    ``nlg_mode=bank``: template from the bank keyed by (intent, placeholder ids),
    chosen by ``(call_id, turn)``; never calls the LLM. ``nlg_mode=llm``: on a
    guard-rejected template after one retry, falls back to ``TEMPLATES``.
    ``LLMUnavailable`` propagates (the orchestrator falls back and audits it).
    ``TEMPLATE_ONLY_INTENTS`` and ``template_override`` always win.
    ``recent_turns`` (``(role, text)``, oldest first, public lines only) is the
    LLM's conversation context; without it, ``last_rep_line`` alone is sent.
    An empty LLM template (reasoning used the whole budget) counts as rejected.
    """
    cfg = settings or get_settings()
    allowed = _allowed_ids(action)
    required = action.required
    template = action.template_override or TEMPLATES[action.intent]

    use_llm = (
        action.intent not in TEMPLATE_ONLY_INTENTS and action.template_override is None
    )
    source = "override" if action.template_override else "default"
    fallback_reason: str | None = None
    if use_llm and cfg.nlg_mode == "bank":
        picked = pick_template(
            action, call_id=call_id, turn=turn, bank=load_bank(cfg.nlg_bank_path)
        )
        if picked is not None:
            template = picked
            source = "bank"
        else:
            fallback_reason = "bank_miss"
    elif use_llm and cfg.nlg_mode == "llm" and llm is not None:
        placeholder_ids = sorted(allowed)
        messages = nlg_messages(
            action.intent, placeholder_ids, last_rep_line, recent_turns=recent_turns
        )
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
                text = (await llm.chat_text("nlg", msgs, NLG_MAX_TOKENS)).strip()

                # Strip accidental fences
                if text.startswith("```"):
                    lines = text.split("\n")
                    text = "\n".join(
                        ln for ln in lines if not ln.strip().startswith("```")
                    ).strip()
                # An empty template passes template_guard when nothing is required
                # and would then speak SAFE_FALLBACK; treat it as a rejection.
                tg = (
                    template_guard(text, allowed, required)
                    if text
                    else GuardResult(ok=False, reason="empty")
                )
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
            except LLMUnavailable:
                # Not swallowed: the orchestrator falls back to TEMPLATES and audits
                # ``llm_unavailable``. Any other exception is a bug and propagates.
                raise
        if candidate:
            tg = template_guard(candidate, allowed, required)
            if tg.ok:
                template = candidate
                source = "llm"
        if source != "llm":
            fallback_reason = "llm_template_rejected"

    if trace_out is not None:
        trace_out["mode"] = cfg.nlg_mode
        trace_out["source"] = source
        if fallback_reason is not None:
            trace_out["fallback_used"] = True
            trace_out["fallback_reason"] = fallback_reason
    return _render_filled(
        template,
        action,
        ref_date,
        creditor_numbers=creditor_numbers,
        private_blocklist=private_blocklist,
        audit=audit,
        call_id=call_id,
        blocked_out=blocked_out,
        trace_out=trace_out,
    )
