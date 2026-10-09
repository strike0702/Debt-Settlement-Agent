"""Conversational acts spoken before the move: acknowledgement and off-script answer.

After ``decide()`` has picked the move, ``attach_acts`` may add two optional
acts that are spoken *before* it, without changing the move:

- ``ack``: PUBLIC facts (source ``creditor``) for the terms the rep settled this
  turn, e.g. "Got it, 6 payments at a $250 minimum.", and (Phase 46b) a
  verified dollar-total ask ("Got it, $420 in total."). A value is echoed only
  if ``rendered_guard`` would already allow it (it is in ``creditor_numbers``)
  and it never collides with the private blocklist.
- ``answer``: a policy-supplied, number-free talking point for an off-script
  question the NLU flagged (``asks_question`` / ``question_topic``). A private
  info ask is never answered: the policy refuses it first (REFUSE_PRIVATE).

Phase 46b also holds the rep-correction cues: ``is_ack_correction`` (the rep
corrects a term we just acknowledged, "No, it's six payments") and
``is_ack_dispute`` ("that's not what I said", no value needed). The
orchestrator applies them to belief; nothing here touches belief.

This module is pure policy-side code (no LLM, no engine). It does not render
speech; ``app.agent.nlg.render_acts`` does, with the same guards. The
orchestrator calls it with ``Settings.nlg_ack`` (ack only, the default) or
``Settings.nlg_h3`` (ack + answer).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.agent.guards import _cross_match
from app.domain.actions import Action, AnswerAct, Intent
from app.domain.belief import BeliefChange, TermStatus
from app.domain.facts import Fact

# Talking points per question topic. Number-free and commitment-free: they go
# through template_guard and rendered_guard like any other line.
ANSWER_POINTS: dict[str, str] = {
    "why_not_higher": (
        "Our offer reflects what the client's settlement program can fund, "
        "so we keep it at a level the client can actually pay."
    ),
    "next_steps": (
        "Next, I send the proposed terms to the client for review, "
        "and we follow up with you once they decide."
    ),
    "who_approves": (
        "The client makes the final decision; I can propose terms on the call, "
        "but only the client can approve them."
    ),
    "timeline": (
        "The client reviews the proposal after this call, "
        "and we follow up with you as soon as they decide."
    ),
    "other": "That is a fair question; I will note it so our team can follow up with you.",
}

# Field → (ack fact id, fact kind). Order is the spoken order.
ACK_FIELDS: dict[str, tuple[str, str]] = {
    "max_payments": ("ack_max_payments", "count"),
    "min_payment_cents": ("ack_min_payment", "money"),
    "first_payment_date": ("ack_first_payment_date", "date"),
}
# A verified dollar-total ask (``VerifiedAnalysis.ask_total_bp`` set) is not a
# belief term; it is acknowledged from the analysis. Spoken first.
ACK_TOTAL_ID = "ack_total"

# Moves that already speak the term (READ_BACK, CLARIFY), refuse, or end the
# call: an ack in front of them is redundant or misleading.
NO_ACK_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.OPENING,
        Intent.READ_BACK,
        Intent.CLARIFY,
        Intent.REFUSE_PRIVATE,
        Intent.REFUSE_COMMIT,
        Intent.SPEAK_SCHEDULE,
        Intent.CLOSE,
        Intent.NO_DEAL_WRAP,
        Intent.ESCALATE,
    }
)
# Moves that end or hand off the call: no answer act in front of them.
NO_ANSWER_INTENTS: frozenset[Intent] = frozenset(
    {Intent.OPENING, Intent.REFUSE_PRIVATE, Intent.CLOSE, Intent.NO_DEAL_WRAP, Intent.ESCALATE}
)
# (topic, move) pairs where the move already says the same thing.
_REDUNDANT_ANSWERS: frozenset[tuple[str, Intent]] = frozenset(
    {
        ("who_approves", Intent.REFUSE_COMMIT),
        ("next_steps", Intent.PROPOSE_WRAP),
        ("timeline", Intent.PROPOSE_WRAP),
    }
)


def ack_facts(
    belief_changes: list[BeliefChange],
    creditor_numbers: set[tuple[str, int | date]],
    private_blocklist: set[tuple[str, int | date]],
) -> dict[str, Fact]:
    """PUBLIC ``ack_*`` facts for terms that became KNOWN (or changed) this turn.

    Hedged / unverified terms stay TENTATIVE and are read back instead, so they
    never reach here. Values not said by the creditor, or matching a private
    figure, are skipped rather than spoken.
    """
    out: dict[str, Fact] = {}
    for ch in belief_changes:
        spec = ACK_FIELDS.get(ch.field)
        if spec is None or ch.new_status != TermStatus.KNOWN:
            continue
        if ch.old_status == TermStatus.KNOWN and ch.old_value == ch.new_value:
            continue
        fid, kind = spec
        value = ch.new_value
        if not isinstance(value, (int, date)) or isinstance(value, bool):
            continue
        if not _cross_match(kind, value, creditor_numbers):
            continue
        if _cross_match(kind, value, private_blocklist):
            continue
        out[fid] = Fact(id=fid, kind=kind, value=value, visibility="PUBLIC", source="creditor")  # type: ignore[arg-type]
    return {fid: out[fid] for fid, _ in ACK_FIELDS.values() if fid in out}


def ack_total_fact(
    analysis: Any,
    creditor_numbers: set[tuple[str, int | date]],
    private_blocklist: set[tuple[str, int | date]],
) -> Fact | None:
    """``ack_total`` for a dollar total that became this turn's ask, else None.

    Only a total that ``resolve_amounts`` turned into the ask (``ask_total_bp``
    set) counts; a held-back or ambiguous amount never does.
    """
    cents = getattr(analysis, "settlement_ask_total_cents", None)
    if getattr(analysis, "ask_total_bp", None) is None or not isinstance(cents, int):
        return None
    if isinstance(cents, bool) or cents <= 0:
        return None
    if not _cross_match("money", cents, creditor_numbers):
        return None
    if _cross_match("money", cents, private_blocklist):
        return None
    return Fact(
        id=ACK_TOTAL_ID, kind="money", value=cents, visibility="PUBLIC", source="creditor"
    )


def acked_fields(ack: dict[str, Fact]) -> dict[str, Any]:
    """Belief field → value for the term acks in ``ack`` (the total is not a field)."""
    by_id = {fid: field for field, (fid, _) in ACK_FIELDS.items()}
    return {by_id[fid]: f.value for fid, f in ack.items() if fid in by_id}


# The rep corrects a value we just acknowledged, i.e. says we misheard. A leading
# "no" only counts together with a new value for an acked field (the
# orchestrator checks that); a bare "no" usually answers our question. "Actually,
# make that 8" is the rep changing their own term, not our misreading: it keeps
# the CONTRADICTED → CLARIFY path.
_ACK_CORRECTION_RE = re.compile(
    r"^\W*(?:no|nope|nah)\b"
    r"|\bI said\b|\bnot what I said\b|\b(?:you )?misheard\b"
    r"|\bI (?:did not|didn't|never) say\b|\bthat'?s (?:wrong|not right|incorrect)\b",
    re.IGNORECASE,
)
# The rep says the ack is wrong without giving the value: specific phrases only,
# so "that's not right for us" about a price does not reopen terms.
_ACK_DISPUTE_RE = re.compile(
    r"\bnot what I (?:said|meant)\b|\bI (?:did not|didn't|never) say\b"
    r"|\byou (?:misheard|misunderstood|got (?:that|it) wrong)\b|\bthat'?s not what I\b",
    re.IGNORECASE,
)


def is_ack_correction(utterance: str) -> bool:
    """True when the line is phrased as a correction ("No, it's six payments")."""
    return _ACK_CORRECTION_RE.search(utterance) is not None


def is_ack_dispute(utterance: str) -> bool:
    """True when the rep says we got it wrong ("that's not what I said")."""
    return _ACK_DISPUTE_RE.search(utterance) is not None


def answer_act(action: Action, analysis: Any) -> AnswerAct | None:
    """Talking point for the NLU-flagged question, or None when it should not attach."""
    if not getattr(analysis, "asks_question", False):
        return None
    if getattr(analysis, "asks_client_private_info", False):
        return None
    if action.intent in NO_ANSWER_INTENTS:
        return None
    topic = getattr(analysis, "question_topic", None) or "other"
    if topic not in ANSWER_POINTS:
        topic = "other"
    if (topic, action.intent) in _REDUNDANT_ANSWERS:
        return None
    return AnswerAct(topic=topic, text=ANSWER_POINTS[topic])


def attach_acts(
    action: Action,
    analysis: Any,
    belief_changes: list[BeliefChange],
    *,
    creditor_numbers: set[tuple[str, int | date]],
    private_blocklist: set[tuple[str, int | date]],
    with_answer: bool = True,
) -> Action:
    """Copy of ``action`` with ``ack`` / ``answer`` set; intent, facts and effects unchanged.

    ``with_answer=False`` is the default agent's code-built ack (``nlg_ack``).
    """
    ack: dict[str, Fact] = {}
    if action.intent not in NO_ACK_INTENTS:
        total = ack_total_fact(analysis, creditor_numbers, private_blocklist)
        if total is not None:
            ack[ACK_TOTAL_ID] = total
        ack.update(ack_facts(belief_changes, creditor_numbers, private_blocklist))
    answer = answer_act(action, analysis) if with_answer else None
    if not ack and answer is None:
        return action
    return action.model_copy(update={"ack": ack, "answer": answer})
