"""Conversational acts spoken before the move: acknowledgement and off-script answer.

After ``decide()`` has picked the move, ``attach_acts`` may add two optional
acts that are spoken *before* it, without changing the move:

- ``ack``: PUBLIC facts (source ``creditor``) for the terms the rep settled this
  turn, e.g. "Got it, 6 payments at a $250 minimum.", and a
  verified dollar-total ask ("Got it, $420 in total."). The
  payment structure is acked too, as a digit-free ``text`` fact ("Got it, a
  balloon schedule."). A value is echoed only
  if ``rendered_guard`` would already allow it (it is in ``creditor_numbers``)
  and it never collides with the private blocklist.
- ``answer``: a policy-supplied, number-free talking point for an off-script
  question the NLU flagged (``asks_question`` / ``question_topic``). A private
  info ask is never answered: the policy refuses it first (REFUSE_PRIVATE).

This module also holds the rep-correction cues: ``is_ack_correction`` (the rep
says we misheard a term we just acknowledged, "I said six payments"; a bare
"No, it's six" is not enough), ``is_change_request`` (the rep asks to change
it, "can you change it to $250", "make that 6") and ``is_ack_dispute``
("that's not what I said", no value needed). The orchestrator applies them to
belief; nothing here touches belief.

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
    "payment_structure": ("ack_payment_structure", "text"),
}
# Spoken phrase per payment structure. Fixed code text, no digits;
# a value outside this map is never acked.
STRUCTURE_ACK_PHRASES: dict[str, str] = {
    "even": "even payments",
    "balloon": "a balloon schedule",
    "flexible": "a flexible schedule",
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
        if kind == "text":
            # Structure words are not figures: no creditor-number cross-match, but
            # only a known structure ever becomes a phrase.
            phrase = STRUCTURE_ACK_PHRASES.get(str(value))
            if phrase is not None:
                out[fid] = Fact(
                    id=fid, kind="text", value=phrase, visibility="PUBLIC", source="creditor"
                )
            continue
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
    by_phrase = {phrase: value for value, phrase in STRUCTURE_ACK_PHRASES.items()}
    out: dict[str, Any] = {}
    for fid, f in ack.items():
        if fid not in by_id:
            continue
        # The structure ack holds the spoken phrase; map it back to the belief value.
        out[by_id[fid]] = by_phrase.get(f.value, f.value) if f.kind == "text" else f.value
    return out


# The rep corrects a value we just acknowledged, i.e. says we misheard: "I said
# 6", "you misheard", "you heard wrong", "that's not what I said", "no, I said
# ...". A change request is the other explicit cue (``is_change_request``). A
# bare "No, it's 6" / "No, actually it's 6" reads just as well as the rep
# changing their own term, so it keeps the CONTRADICTED -> CLARIFY path; the
# orchestrator also needs a new value for an acked field.
_ACK_CORRECTION_RE = re.compile(
    r"\bI (?:said|told you)\b|\bnot what I (?:said|meant)\b"
    r"|\b(?:you )?(?:misheard|misunderstood)\b"
    r"|\byou (?:heard|got) (?:(?:that|it|me) )?wrong\b"
    r"|\bI (?:did not|didn't|never) say\b",
    re.IGNORECASE,
)
# The rep says the ack is wrong without giving the value: specific phrases only,
# so "that's not right for us" about a price does not reopen terms.
_ACK_DISPUTE_RE = re.compile(
    r"\bnot what I (?:said|meant)\b|\bI (?:did not|didn't|never) say\b"
    r"|\byou (?:misheard|misunderstood|(?:got|heard) (?:(?:that|it|me) )?wrong)\b"
    r"|\bthat'?s not what I\b",
    re.IGNORECASE,
)


# The rep asks us to change a value we just acknowledged: "can you change it to
# $250", "change that to 6", "make it $250", "make that 6", "let's update it to
# ...", "switch it to ...". "make it / that" needs a value right after it, so
# "can you make that work" is not a cue.
_NUMBER_START = (
    r"(?:\$|\d|(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty"
    r"|forty|fifty|sixty|seventy|eighty|ninety|hundred|january|february|march"
    r"|april|may|june|july|august|september|october|november|december)\b)"
)
_CHANGE_REQUEST_RE = re.compile(
    r"\b(?:change|update|switch|set|move|bump)\s+(?:it|that|this)\s+(?:(?:up|down|back)\s+)?to\b"
    r"|\bmake\s+(?:it|that|this)\s+(?=(?:(?:a|an|the)\s+)?(?:(?:maximum|minimum|max|min)\s+of\s+"
    r"|at\s+(?:least|most)\s+|up\s+to\s+|only\s+)?" + _NUMBER_START + r")",
    re.IGNORECASE,
)
# A change cue inside a question counts only as a request ("can you change it
# to $250?"); "should we change it to ...?" / "why did you change it to ...?"
# asks about it instead.
_REQUEST_OPENER_RE = re.compile(
    r"\b(?:(?:can|could|would|will)\s+you|(?:can|could)\s+we|please"
    r"|(?:is|would)\s+it\s+be\s+possible|is\s+it\s+possible|any\s+chance)\b",
    re.IGNORECASE,
)
# Negators and conditionals that cancel the cue in its clause ("we can't change
# it to 6", "if we change it to 6"). A bare "no" is not one: "No, change it to 6".
_CHANGE_GUARD_RE = re.compile(
    r"\b(?:not|never|cannot|unless|if|whether|\w+n['\u2019]t"
    r"|(?:do|does|did|is|are|was|were|ca|wo|would|could|should)nt)\b",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
_CLAUSE_BREAK_RE = re.compile(r"[.;,:!?]|\b(?:but|though|although|however)\b", re.IGNORECASE)
_QUESTION_OPENER_RE = re.compile(
    r"^\s*(?:is|are|was|were|does|did|has|have|had|would|could|will|shall|should"
    r"|(?:do|can|why|how|what)\b)",
    re.IGNORECASE,
)


def is_change_request(utterance: str) -> bool:
    """True when the rep asks to change a value ("can you change it to $250", "make that 6").

    Not when the cue is negated or conditional in its clause, or sits in a
    question that is not a request ("should we change it to 6?").
    """
    for m in _CHANGE_REQUEST_RE.finditer(utterance):
        head = utterance[: m.start()]
        stops = list(_SENTENCE_END_RE.finditer(head))
        sentence_head = head[stops[-1].end() if stops else 0 :]
        tail = _SENTENCE_END_RE.search(utterance, m.end())
        asked = (
            (tail is not None and tail.group() == "?")
            or (tail is None and utterance.rstrip().endswith("?"))
            or _QUESTION_OPENER_RE.match(sentence_head) is not None
        )
        if asked and not _REQUEST_OPENER_RE.search(sentence_head):
            continue
        clause = _CLAUSE_BREAK_RE.split(head)[-1]
        if _CHANGE_GUARD_RE.search(clause):
            continue
        return True
    return False


def is_ack_correction(utterance: str) -> bool:
    """True when the line says we misheard ("I said six", "you misheard"); a bare "no" is not."""
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
