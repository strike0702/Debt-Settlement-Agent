"""Code-side cue rules that repair the model's stance and side-channel flags.

Each ``repair_*`` takes what the model claimed and the rep's line and returns
the value the agent will act on: an unguarded accept / reject phrase or a
dominant short ack sets the stance (never accept on a prompt injection); a
phrase inside a question, after a conditional or after a negator never forces
a label. Flags (wants to end, firm, private-info ask, commitment demand,
question topic, revision) are the model's flag OR an un-negated regex cue.
``hostility_floor_hit`` lifts clear insults / threats to ``HOSTILITY_FLOOR``.
``_repair_dispositions`` applies them all in a fixed order; called from
``verify.post_verify`` and the oracle path of ``llm_call.analyze``. Regex
only: no LLM, no term values.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.nlu.quotes import normalize_for_quote
from app.agent.nlu_types import TurnAnalysis
from app.agent.numbers import extract_tokens
from app.domain.nlu_types import QUESTION_TOPICS

_READBACK_CONFIRM_RE = re.compile(
    r"\b(?:"
    r"yes"
    r"|correct"
    r"|that's right"
    r"|thats right"
    r"|that is correct"
    r"|confirmed"
    r"|confirm"
    r")\b",
    re.IGNORECASE,
)
_READBACK_DENY_RE = re.compile(
    r"\b(?:"
    r"no"
    r"|incorrect"
    r"|that's wrong"
    r"|thats wrong"
    r"|not correct"
    r"|deny"
    r")\b",
    re.IGNORECASE,
)


def _verify_readback_response(claimed: str | None, utterance: str) -> str | None:
    """Keep confirm/deny only when the utterance contains a matching phrase."""
    if claimed == "confirm" and _READBACK_CONFIRM_RE.search(utterance):
        return "confirm"
    if claimed == "deny" and _READBACK_DENY_RE.search(utterance):
        return "deny"
    return None


# Deterministic stance repair when the LLM mislabels clear accept/reject lines.
# Explicit accept phrases force accept anywhere in the line; bare acknowledgements
# (``_ACK_WORDS``) only when they dominate the line (see ``_ack_dominant``).
_ACCEPT_STANCE_RE = re.compile(
    r"\b(?:"
    r"agreed"
    r"|we agree"
    r"|that works"
    r"|sounds good"
    r"|we can accept"
    r"|we accept"
    r"|schedule works"
    r"|payment schedule works"
    r")\b",
    re.IGNORECASE,
)
_ACK_WORDS = frozenset(
    {"ok", "okay", "yes", "yeah", "yep", "fine", "sure", "cool", "alright", "right"}
)
# Words that neither acknowledge nor add content ("yeah that's fine", "sure thing").
_ACK_NEUTRAL = frozenset(
    {"uh", "um", "oh", "well", "so", "thing", "that", "thats", "its", "is", "then",
     "great", "good", "perfect", "with", "me"}
)
# Instruction-shaped text aimed at the model; such a line is never an accept.
_INJECTION_RE = re.compile(
    r"(?:"
    r"\bignore (?:all |your |the |any )?(?:previous |prior |above )?(?:instructions|rules)\b"
    r"|\bdisregard (?:all |your |the |any )?(?:previous |prior )?(?:instructions|rules)\b"
    r"|\brepeat after me\b"
    r"|^\s*(?:system|developer|assistant)\s*[:,]"
    r"|\bdeveloper mode\b"
    r"|\bset stance\b"
    r"|\boutput stance\b"
    r"|\blabel every\b"
    # Forged prompt markup / NLU JSON in the line (corpus i06).
    r"|</?\s*utterance\s*>"
    r"|\"stance\"\s*:"
    r")",
    re.IGNORECASE,
)
_REJECT_STANCE_RE = re.compile(
    r"\b(?:"
    r"too low"
    r"|does not work"
    r"|doesn't work"
    r"|cannot go below"
    r"|can't go below"
    r"|cannot go lower"
    r"|can't go lower"
    r"|minimum is actually"
    r"|those payment amounts are off"
    r")\b",
    re.IGNORECASE,
)
_WANTS_TO_END_RE = re.compile(
    r"(?:"
    r"\bthanks\b"
    r"|\bthank you\b"
    r"|\bthx\b"
    r"|\bbye\b"
    r"|\bgoodbye\b"
    r"|\bgood bye\b"
    r"|\bthat's all\b"
    r"|\bthats all\b"
    r"|\bthat is all\b"
    r"|\bnothing else\b"
    r"|\bwe'?re (?:all )?done\b"
    r"|\bwe are (?:all )?done\b"
    r"|\bend (?:the )?(?:call|chat)\b"
    r")",
    re.IGNORECASE,
)
_ASKS_FOR_SCHEDULE_RE = re.compile(
    r"(?:"
    r"\bschedule\b"
    r"|\bpayment dates?\b"
    r"|\bper[- ]date\b"
    r"|\beach payment\b"
    r"|\bby date\b"
    r"|\bbreak(?:down)? (?:the )?payments?\b"
    r"|\blist (?:the )?payments?\b"
    r"|\bdates? (?:and|for) (?:the )?amounts?\b"
    r")",
    re.IGNORECASE,
)
_FIRM_RE = re.compile(
    r"(?:"
    r"\bfinal (?:offer|number)\b"
    r"|\blowest we can (?:go|do)\b"
    r"|\b(?:cannot|can ?not|can't|won't) go (?:any )?lower\b"
    r"|\bour floor\b"
    r"|\bis the floor\b"
    r"|\bbottom line\b"
    r"|\bnon-?negotiable\b"
    r"|\btake it or leave it\b"
    r"|\bbest (?:we|i) can do\b"
    r")",
    re.IGNORECASE,
)
# Post-proposal revision cues. Do NOT include "actually" / "make that" —
# the contradictory persona uses those for false flips during discovery.
_REVISION_RE = re.compile(
    r"(?:"
    r"\binstead\b"
    r"|\brather\b"
    r"|\bhow about\b"
    r"|\bwhat about\b"
    r"|\bcan we do\b"
    r"|\bcould we do\b"
    r"|\bcan (?:you|we) make it\b"
    r"|\blet'?s do\b"
    r"|\bchange (?:it|that|this) to\b"
    r"|\bswitch to\b"
    r")",
    re.IGNORECASE,
)
# LLM side-channel flags — keep only when the utterance corroborates.
_HOSTILITY_RE = re.compile(
    r"(?:"
    r"\b(?:idiot|stupid|incompetent|moron|dumb)\b"
    r"|\bshut up\b"
    r"|\bwaste of (?:my )?time\b"
    r"|\blawsuit\b"
    r"|\bsue (?:you|us)\b"
    r"|\bhostile\b"
    r"|\babusive\b"
    r"|\bf+u+c?k+(?:ing)?\b"
    r"|\basshole\b"
    r")",
    re.IGNORECASE,
)
# "client's account" alone is usually the creditor's own account, so only
# account balance / number count.
_PRIVATE_INFO_RE = re.compile(
    r"(?:"
    r"\b(?:client'?s?|their|his|her)\s+"
    r"(?:bank\s+)?(?:balance|income|draft|ssn|salary|paycheck|routing"
    r"|account\s+(?:balance|number))\b"
    r"|\b(?:bank balance|monthly income|social security|ssn|routing number)\b"
    r"|\bdraft amount\b"
    r"|\bwhat (?:is|are) (?:the )?client\b"
    # Money held for the client (escrow / program /
    # dedicated / savings account), "the most they could pay", SSN digits.
    # The creditor's own account ("this account", "the account number for this
    # debt") and the balance owed never match.
    r"|\bescrow\b"
    r"|\b(?:dedicated|settlement|program|savings|checking|trust|special[- ]purpose)\s+account\b"
    r"|\bthe most (?:that )?(?:they|he|she|(?:the|your) (?:client|consumer|debtor|borrower"
    r"|customer|guy))\s+(?:could|can|would|might)\s+(?:(?:be able to|realistically|really"
    r"|possibly|actually)\s+)?(?:pay|afford|do|put|offer|manage|come up with)\b"
    r"|\b(?:client|consumer|debtor|borrower|customer|their|his|her)'?s?\s+social\b(?!\s+media)"
    r"|\blast (?:four|4)(?: digits)? of (?:the |their |his |her |your )?\w*'?s?\s*(?:social|ssn)\b"
    r"|\bss#"
    r")",
    re.IGNORECASE,
)
# Clearly hostile phrases set hostility to the floor
# (``HOSTILITY_FLOOR``, at or above any escalation threshold) even when the
# model scores the line low. Negated ("not a waste of time"), quoted ("he said
# 'shut up'") and hypothetical ("if this is a scam") uses do not count; firm
# pushback and plain frustration ("this is taking a while") never match.
HOSTILITY_FLOOR = 1.0
_HOSTILITY_FLOOR_RE = re.compile(
    r"(?:"
    r"\b(?:idiots?|stupid|moron(?:s|ic)?|incompetent|dumb(?:est)?|clowns?|asshole)\b"
    r"|\bshut up\b"
    r"|\b(?:waste|wasting) (?:of )?(?:my|our) time\b"
    r"|\breport (?:you|your (?:firm|company|agency|office|outfit|people))\b"
    r"|\breport (?:this|it) to the\b"
    r"|\b(?:is|are|it'?s|this is|what) a scam\b|\bscam(?:ming|mers?)\b"
    r"|\byou(?:'re| are) (?:crooks|frauds|liars|thieves)\b"
    r")",
    re.IGNORECASE,
)
_DEMANDS_COMMITMENT_RE = re.compile(
    r"(?:"
    r"\bcommit(?:ment)?\b"
    r"|\block(?:ed)? in\b"
    r"|\bguarantee (?:this|that|it|the)\b"
    r"|\bfirm (?:yes|commitment|deal)\b"
    r"|\bsign (?:today|now)\b"
    r"|\bbinding (?:today|now|agreement)\b"
    r")",
    re.IGNORECASE,
)


def _ack_dominant(utterance: str) -> bool:
    """True when acknowledgement words outnumber content words and no number appears."""
    if extract_tokens(utterance):
        return False
    words = normalize_for_quote(utterance.replace("all right", "alright")).split()
    acks = sum(w in _ACK_WORDS for w in words)
    content = sum(w not in _ACK_WORDS and w not in _ACK_NEUTRAL for w in words)
    return acks > 0 and acks > content


# Negators that cancel an accept phrase later in the same clause ("I'm not sure
# that works", "nothing has been agreed"). ``n't`` forms also match without the
# apostrophe (STT drops it) and with a curly one.
_ACCEPT_NEGATOR_RE = re.compile(
    r"\b(?:not|no|never|nothing|nobody|neither|nor|hardly|unsure|cannot"
    r"|\w+n['\u2019]t"
    r"|(?:do|does|did|is|are|was|were|ca|wo|would|could|should|has|have|had|ai)nt)\b",
    re.IGNORECASE,
)
# Reject phrases: same negators minus a bare "no" ("No that's too low" without
# a comma still rejects).
_REJECT_NEGATOR_RE = re.compile(
    r"\b(?:not|never|nothing|nobody|neither|nor|hardly|cannot"
    r"|\w+n['\u2019]t"
    r"|(?:do|does|did|is|are|was|were|ca|wo|would|could|should|has|have|had|ai)nt)\b",
    re.IGNORECASE,
)
# Firm phrases: "can't / cannot / won't" usually belong to the firm statement
# itself ("we can't go below our floor"), so they do not negate it.
_FIRM_NEGATOR_RE = re.compile(
    r"\b(?:not|no|never|nothing|neither|nor"
    r"|isn['\u2019]?t|aren['\u2019]?t|wasn['\u2019]?t|weren['\u2019]?t"
    r"|wouldn['\u2019]?t|shouldn['\u2019]?t|couldn['\u2019]?t"
    r"|don['\u2019]?t|doesn['\u2019]?t|didn['\u2019]?t|ain['\u2019]?t)\b",
    re.IGNORECASE,
)
# Idioms that start with a negator but affirm ("No problem, that works").
_NEGATOR_IDIOM_RE = re.compile(
    r"\b(?:no (?:problem|problems|worries|doubt)|not a problem)\b", re.IGNORECASE
)
# Clause boundaries: sentence / clause punctuation, or a contrastive conjunction.
_CLAUSE_BREAK_RE = re.compile(r"[.;,:!?]|\b(?:but|though|although|however)\b", re.IGNORECASE)
# A phrase after one of these in its clause is hypothetical, not a statement
# ("Let me check if that works", "Before anything is agreed, ...").
_CONDITIONAL_RE = re.compile(r"\b(?:if|whether|unless|before|until|once)\b", re.IGNORECASE)
# Sentence end: punctuation followed by space or end (not the dot in "57.5").
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
# Auxiliary openers that make a sentence a question even when STT drops the "?".
_QUESTION_OPENER_RE = re.compile(
    r"^\s*(?:is|are|was|were|does|did|has|have|had|would|could|will|shall|should"
    r"|(?:do|can) (?:we|you|they|i))\b",
    re.IGNORECASE,
)


def _in_question(utterance: str, start: int, end: int) -> bool:
    """True when the sentence holding ``utterance[start:end]`` is a question.

    A question ends in "?" or opens with an auxiliary ("Is that agreed").
    """
    head = utterance[:start]
    stops = list(_SENTENCE_END_RE.finditer(head))
    sent_start = stops[-1].end() if stops else 0
    tail = _SENTENCE_END_RE.search(utterance, end)
    if tail is not None and tail.group() == "?":
        return True
    if tail is None and utterance.rstrip().endswith("?"):
        return True
    return _QUESTION_OPENER_RE.match(utterance[sent_start:start]) is not None


def _phrase_guarded(utterance: str, m: re.Match[str], negators: re.Pattern[str]) -> bool:
    """True when a phrase match must not decide: question, conditional or negated clause.

    Only the clause text before the match counts for the conditional and the
    negator, so "That works, nothing else to add." still accepts. The LLM's own
    reading decides a guarded line.
    """
    if _in_question(utterance, m.start(), m.end()):
        return True
    clause = _CLAUSE_BREAK_RE.split(utterance[: m.start()])[-1]
    clause = _NEGATOR_IDIOM_RE.sub(" ", clause)
    return bool(_CONDITIONAL_RE.search(clause) or negators.search(clause))


def _has_unguarded_accept_phrase(utterance: str) -> bool:
    """True when some accept phrase is a plain statement (not asked, conditional or negated)."""
    return any(
        not _phrase_guarded(utterance, m, _ACCEPT_NEGATOR_RE)
        for m in _ACCEPT_STANCE_RE.finditer(utterance)
    )


def _has_unguarded_reject_phrase(utterance: str) -> bool:
    """True when some reject phrase is a plain statement ("That's not too low" is not)."""
    return any(
        not _phrase_guarded(utterance, m, _REJECT_NEGATOR_RE)
        for m in _REJECT_STANCE_RE.finditer(utterance)
    )


def repair_stance(stance: str, utterance: str, *, has_terms: bool = False) -> str:
    """Override LLM stance when the utterance clearly accepts or rejects.

    Order: injection (never accept) → reject phrase → accept phrase → short
    acknowledgement that dominates the line and carries no number or term.
    A phrase inside a question, after a conditional ("if", "whether", "before")
    or after a negator in its clause never forces anything; the LLM decides.
    """
    if _INJECTION_RE.search(utterance):
        return "other" if stance == "accept" else stance
    if _has_unguarded_reject_phrase(utterance):
        return "reject"
    if _has_unguarded_accept_phrase(utterance):
        return "accept"
    if not has_terms and _ack_dominant(utterance):
        return "accept"
    return stance


def _flag_or_regex(claimed: bool, pattern: re.Pattern[str], utterance: str) -> bool:
    return bool(claimed) or pattern.search(utterance) is not None


# Regex cues the rep disclaims ("no need for the client's balance") or says of
# themselves ("we commit to holding this") are not asks. Checked on the clause
# text just before the cue; the LLM flag is not vetoed (it can read negation).
_CUE_NEGATED_RE = re.compile(
    r"(?:\b(?:no|not|don'?t|do not|never|won'?t|without|regardless of)\b"
    r"|\b(?:we|i|i'll|we'll|i will|we will)\s+$)",
    re.IGNORECASE,
)


def _flag_or_unnegated_cue(claimed: bool, pattern: re.Pattern[str], utterance: str) -> bool:
    if claimed:
        return True
    for m in pattern.finditer(utterance):
        clause = re.split(r"[.,;?!]", utterance[max(0, m.start() - 40) : m.start()])[-1]
        if not _CUE_NEGATED_RE.search(clause):
            return True
    return False


def repair_wants_to_end(wants_to_end: bool, utterance: str) -> bool:
    """Set wants_to_end when the rep clearly closes (thanks / bye / done)."""
    return _flag_or_regex(wants_to_end, _WANTS_TO_END_RE, utterance)


def repair_asks_for_schedule(asks: bool, utterance: str) -> bool:
    """True when the rep asks for payment dates / schedule detail."""
    return _flag_or_regex(asks, _ASKS_FOR_SCHEDULE_RE, utterance)


def repair_firm(firm: bool, utterance: str) -> bool:
    """LLM flag OR a firm phrase stated plainly (floor / final / cannot go lower).

    A phrase inside a question ("Is that your final offer?"), after a
    conditional ("If that were our floor") or after a negator in its clause
    ("not the lowest we can go") does not set the flag.
    """
    if firm:
        return True
    return any(
        not _phrase_guarded(utterance, m, _FIRM_NEGATOR_RE)
        for m in _FIRM_RE.finditer(utterance)
    )


# A question needs a "?" or an interrogative opener; a statement is never answered.
_QUESTION_CUE_RE = re.compile(
    r"\?|^\s*(?:why|who|when|what|how|where|can you|could you|will you)\b",
    re.IGNORECASE,
)
# Topic cues, checked in order. A cue alone also flags the question (LLM miss).
_QUESTION_TOPIC_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "why_not_higher",
        re.compile(
            r"\bwhy\b[^?.]*\b(?:higher|more|so low|better|low ?ball)\b"
            r"|\b(?:can't|cannot|won't) you (?:go|do|offer) (?:any )?(?:higher|more|better)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "who_approves",
        re.compile(
            r"\bwho\b[^?.]*\b(?:approv\w*|signs?|sign off|decides?|authori[sz]\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "next_steps",
        re.compile(
            r"\b(?:next steps?|what happens (?:next|now|after)|what now|what comes next)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "timeline",
        re.compile(
            r"\b(?:how long|how soon|by when|time ?line|time ?frame"
            r"|when (?:will|would|do|does|can) (?:i|we|you) (?:hear|get|know))\b",
            re.IGNORECASE,
        ),
    ),
)


# An "other" question about terms or figures is on-script: the move answers it.
_ON_SCRIPT_RE = re.compile(
    r"\b(?:percent\w*|settle\w*|payments?|schedule|minimum|balance|installments?"
    r"|offer|dates?|amount|counter|total|tiers?)\b|[$%\d]",
    re.IGNORECASE,
)


def _question_topic_cue(utterance: str) -> str | None:
    for topic, pattern in _QUESTION_TOPIC_RES:
        if pattern.search(utterance):
            return topic
    return None


def repair_question(
    asks_question: bool,
    topic: str | None,
    utterance: str,
    *,
    asks_private: bool,
) -> tuple[bool, str | None]:
    """``(asks_question, question_topic)`` after deterministic checks.

    A private-info ask is never a question to answer (policy refuses it first).
    An LLM flag stands only with a question cue; a topic regex cue flags on its
    own. Topic: the regex cue beats an LLM ``other``; unknown topics → ``other``.
    An ``other`` question that mentions terms or figures is on-script (the move
    answers it), so it is dropped.
    """
    if asks_private:
        return False, None
    cue = _question_topic_cue(utterance)
    claimed = asks_question and _QUESTION_CUE_RE.search(utterance) is not None
    if not claimed and cue is None:
        return False, None
    if topic not in QUESTION_TOPICS or topic == "other":
        topic = cue or "other"
    if topic == "other" and _ON_SCRIPT_RE.search(utterance):
        return False, None
    return True, topic


def repair_revises_terms(utterance: str) -> bool:
    """True when the utterance cues a post-proposal term change."""
    return _REVISION_RE.search(utterance) is not None


_QUOTE_CHARS = "\"\u201c\u201d"


def _in_quotes(utterance: str, start: int) -> bool:
    """True when an odd number of double quotes opens before ``start`` (reported speech)."""
    return sum(utterance[:start].count(q) for q in _QUOTE_CHARS) % 2 == 1


def hostility_floor_hit(utterance: str) -> bool:
    """True when a floor phrase is said as a plain statement or a direct question.

    Unlike the stance guards, a question still counts ("Are you stupid?"); a
    negator or a conditional earlier in the clause, or quotes, do not.
    """
    for m in _HOSTILITY_FLOOR_RE.finditer(utterance):
        if _in_quotes(utterance, m.start()):
            continue
        clause = _CLAUSE_BREAK_RE.split(utterance[: m.start()])[-1]
        if _CONDITIONAL_RE.search(clause) or _FIRM_NEGATOR_RE.search(clause):
            continue
        return True
    return False


def repair_hostility(hostility: float, utterance: str) -> float:
    """Keep LLM hostility only when the utterance has hostile cues; else 0.

    A floor phrase (``hostility_floor_hit``) lifts it to ``HOSTILITY_FLOOR``
    whatever the model scored.
    """
    h = max(0.0, min(1.0, float(hostility)))
    if hostility_floor_hit(utterance):
        return max(h, HOSTILITY_FLOOR)
    if h <= 0.0:
        return 0.0
    if _HOSTILITY_RE.search(utterance) is None:
        return 0.0
    return h


def repair_asks_client_private_info(claimed: bool, utterance: str) -> bool:
    """LLM flag OR un-negated regex cue: the rep asks for client financials."""
    return _flag_or_unnegated_cue(claimed, _PRIVATE_INFO_RE, utterance)


def repair_demands_commitment(claimed: bool, utterance: str) -> bool:
    """LLM flag OR un-negated regex cue: the rep demands a lock-in / commitment."""
    return _flag_or_unnegated_cue(claimed, _DEMANDS_COMMITMENT_RE, utterance)


def _repair_dispositions(
    src: TurnAnalysis, utterance: str, *, has_terms: bool
) -> dict[str, Any]:
    """Repaired stance + side-channel flags from an LLM or oracle analysis."""
    return {
        "stance": repair_stance(src.stance, utterance, has_terms=has_terms),
        "readback_response": _verify_readback_response(src.readback_response, utterance),
        "asks_client_private_info": repair_asks_client_private_info(
            src.asks_client_private_info, utterance
        ),
        "demands_commitment": repair_demands_commitment(src.demands_commitment, utterance),
        "hostility": repair_hostility(src.hostility, utterance),
        "wants_to_end": repair_wants_to_end(src.wants_to_end, utterance),
        "asks_for_schedule": repair_asks_for_schedule(src.asks_for_schedule, utterance),
        "firm": repair_firm(src.firm, utterance),
    }
