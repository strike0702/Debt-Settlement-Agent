"""Code-side cue rules that repair the model's stance and side-channel flags.

Each ``repair_*`` takes what the model claimed and the rep's line and returns
the value the agent will act on: an unguarded accept / reject phrase or a
dominant short ack sets the stance (never accept on a prompt injection); a
phrase inside a question, after a conditional or after a negator never forces
a label. Flags (wants to end, firm, private-info ask, commitment demand,
question topic, revision) are the model's flag OR an un-negated regex cue.
``repair_defines_field`` keeps a rule announcement ("before that I need to
define the tiers", "there are three tiers") only with that field's word in
the line; ``repair_list_done`` reads "that's all" at the end of a list.
``hostility_floor_hit`` lifts clear insults / threats to ``HOSTILITY_FLOOR``.
``repair_acts`` applies them all to a turn's acts in a fixed order (model and
oracle acts alike, from ``acts.verify_acts``). Regex only: no LLM, no term
values.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.nlu.quotes import normalize_for_quote
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
    r"|\blowest (?:we|i) can (?:go|do)\b"
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
    # 71a-2: what the client has free after living costs ("left after rent",
    # "after bills", "disposable", "spare each month").
    r"|\bafter (?:(?:paying|covering) )?(?:(?:the|their|his|her) )?"
    r"(?:rent|bills|expenses|living expenses|groceries|utilities)\b"
    r"|\bdisposable\b"
    r"|\bspare (?:cash|money|income)\b"
    r"|\b(?:have|got) (?:\w+ )?(?:to )?spare\b"
    r"|\bspare (?:each|every|a|per) (?:month|week|paycheck)\b"
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


def repair_stance(
    stance: str, utterance: str, *, has_terms: bool = False, opening: bool | None = None
) -> str:
    """Override LLM stance when the utterance clearly accepts or rejects.

    Order: injection (never accept) → reject phrase → accept phrase → short
    acknowledgement that dominates the line and carries no number or term.
    A phrase inside a question, after a conditional ("if", "whether", "before")
    or after a negator in its clause never forces anything; the LLM decides.

    Offer vs counter (71a-2, one rule for both schemas): a price is ``offer``
    only when the rep speaks first (``opening``); once the agent has spoken,
    every price line is ``counter``. ``opening=None`` (callers without the
    agent's line) leaves the label alone. The policy treats both alike.
    """
    if opening is False and stance == "offer":
        stance = "counter"
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


# "six payments max, not eight", "$200 now instead of $150": a negator right
# before a value names the value being replaced.
_NEGATED_VALUE_RE = re.compile(
    r"\b(?:not|no longer|instead of|rather than|isn'?t)\s+"
    r"((?:[\w$'-]+(?:[.,]\d+)*\s+){0,3}[\w$'-]+(?:[.,]\d+)*)",
    re.IGNORECASE,
)


def negates_value(utterance: str, value: Any, *, ref: Any = None) -> bool:
    """True when the line negates ``value`` itself ("..., not eight" with value 8).

    A rule change after a proposal that names the old value it replaces is a
    revision, not a contradiction to clarify (71a-2 held-out
    ``neg_rule_change`` #2). Code reads the numbers (``extract_tokens``); a
    value that is not a number or date never matches.
    """
    if value is None or isinstance(value, (bool, list, dict)):
        return False
    for m in _NEGATED_VALUE_RE.finditer(utterance):
        if any(tok.value == value for tok in extract_tokens(m.group(1), ref=ref)):
            return True
    return False


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


# Rule announcements: the rep says they will give a rule before giving it.
# The field word decides the field (tiers first: "tiered minimums" is tiers,
# not the minimum payment); an announcement needs a cue verb or "there are /
# we will have" in the same clause, or a count right before a tier / level word.
_DEFINES_FIELD_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("min_payment_tiers", re.compile(r"\btier(?:s|ed)?\b", re.IGNORECASE)),
    ("max_token_pays", re.compile(r"\btoken(?:\s+payments?)?\b", re.IGNORECASE)),
    ("max_segments", re.compile(r"\b(?:payment\s+)?(?:levels?|segments?)\b", re.IGNORECASE)),
    (
        "first_payment_date",
        re.compile(r"\b(?:start(?:ing)? date|first payment date|due date)\b", re.IGNORECASE),
    ),
    (
        "max_payments",
        re.compile(
            r"\b(?:number of payments|payment count|how many payments"
            r"|max(?:imum)? (?:number of )?payments)\b",
            re.IGNORECASE,
        ),
    ),
    ("min_payment_cents", re.compile(r"\bminimum(?:\s+(?:payment|amount))?\b", re.IGNORECASE)),
    ("payment_structure", re.compile(r"\b(?:payment\s+)?structure\b", re.IGNORECASE)),
)
_ANNOUNCE_RE = re.compile(
    r"\b(?:define|set out|set up|lay out|tell you|give you|explain|go over|go through"
    r"|walk you through|outline|describe|specify|list|break down|spell out"
    r"|there (?:are|will be|is)|there'?s|we (?:have|will have|'ll have)|we'll have"
    r"|it has|has)\b",
    re.IGNORECASE,
)
_COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_EXPECTED_COUNT_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(_COUNT_WORDS) + r")\s+"
    r"(?:(?:different|distinct|separate|payment|minimum)\s+)*(?:tiers?|levels?|segments?)\b",
    re.IGNORECASE,
)
# "no tiers", "there aren't any tiers": a statement that the rule is empty.
_DEFINES_NEGATED_RE = re.compile(
    r"\b(?:no|not|n't|without|zero|none|never)\b", re.IGNORECASE
)


def _clause_before(utterance: str, end: int) -> str:
    """Text of the clause that ends at ``end`` (after the last . , ; ? ! or "but")."""
    return _CLAUSE_BREAK_RE.split(utterance[:end])[-1]


def _expected_count(utterance: str) -> int | None:
    m = _EXPECTED_COUNT_RE.search(utterance)
    if m is None:
        return None
    raw = m.group(1).lower()
    n = _COUNT_WORDS.get(raw) or int(raw)
    return n if 1 <= n <= 10 else None


def repair_defines_field(
    claimed: str | None,
    claimed_count: int | None,
    utterance: str,
    *,
    given_fields: set[str],
) -> tuple[str | None, int | None]:
    """``(defines_field, expected_count)`` after the code-side checks.

    A field word must be in the line, un-negated in its clause ("there are no
    tiers" states an empty rule). The model's claim stands with its word; with
    no claim, a cue verb / "there are" or a count before "tiers" in the same
    clause sets it. A scalar rule given in this same line (``given_fields``)
    is not pending, so it clears; tiers stay pending (they may come one by
    one). The count is read from the line ("three tiers"); the model's count
    stands only if that number is in the line.
    """
    found: str | None = None
    for fname, pattern in _DEFINES_FIELD_WORDS:
        if claimed is not None and fname != claimed:
            continue
        for m in pattern.finditer(utterance):
            clause = _clause_before(utterance, m.start())
            if _DEFINES_NEGATED_RE.search(clause):
                continue
            counted = fname in ("min_payment_tiers", "max_segments") and _EXPECTED_COUNT_RE.search(
                utterance[max(0, m.start() - 30) : m.end()]
            )
            if claimed is not None or _ANNOUNCE_RE.search(clause) or counted:
                found = fname
                break
        if found is not None:
            break
    if found is None or (found != "min_payment_tiers" and found in given_fields):
        return None, None
    count = _expected_count(utterance) if found in ("min_payment_tiers", "max_segments") else None
    if count is None and claimed_count is not None and 1 <= claimed_count <= 10:
        said = {t.value for t in extract_tokens(utterance) if t.kind == "count"}
        count = claimed_count if claimed_count in said else None
    return found, count


_LIST_DONE_RE = re.compile(
    r"\b(?:that'?s (?:all|it|everything|the last(?: one)?)|that is (?:all|it|everything)"
    r"|those are (?:all|the only ones?)|no (?:more|other|further)(?: tiers?)?"
    r"|nothing (?:else|more)|that was the last(?: one)?|(?:the )?last one)\b"
    r"|^\s*(?:no|nope)[.!]?\s*$",
    re.IGNORECASE,
)


def repair_list_done(utterance: str) -> bool:
    """True when the rep says a list is finished ("that's all", "no more tiers", "no")."""
    return _LIST_DONE_RE.search(utterance) is not None


def _cue_quote(utterance: str) -> str:
    """Quote for an act code adds from a cue: the line itself (acts need a quote)."""
    return (utterance.strip() or "-")[:300]




def repair_acts(
    acts: list[Any],
    utterance: str,
    *,
    has_terms: bool,
    given_fields: set[str],
    collecting: bool,
    model_ended: bool,
) -> tuple[list[Any], int | None]:
    """The turn's non-content acts after the code-side cue rules, and a level count.

    The code-side cue rules, applied to acts (order matters):

    1. accept / reject: an injection drops every accept; else an unguarded
       reject phrase makes the line a reject, an unguarded accept phrase (or a
       short ack that dominates a line with no terms) an accept.
    2. read-back reply: kept only with a matching yes / no phrase.
    3. private-info ask, commitment demand: the model's act OR an un-negated cue.
    4. hostility: kept only with a hostile cue; a floor phrase makes it strong.
    5. closing: the model's ``end_call`` OR a thanks / bye cue; dropped when the
       line finishes a list being collected ("that's all the tiers").
    6. schedule detail: a schedule cue adds ``question`` topic ``schedule``.
    7. firmness: a firm cue makes the price act ``final`` (a number-free final
       price act when the line has none).
    8. off-script questions (``repair_question``), rule announcements
       (``repair_defines_field``) and "that's all" (``list_done``).

    Returns ``(acts, levels)``: ``levels`` is a count of payment levels said as
    the level limit itself ("at most two payment levels"), which the caller
    turns into the ``max_segments`` rule.
    """
    from app.domain.nlu_types import (
        HOSTILITY_LEVEL,
        AnnounceRulesAct,
        AskPrivateInfoAct,
        DemandCommitmentAct,
        EndCallAct,
        HostilityAct,
        ListDoneAct,
        PriceAskAct,
        QuestionAct,
        RespondToOfferAct,
    )

    q = _cue_quote(utterance)
    out = list(acts)

    def _without(type_name: str, **match: Any) -> list[Any]:
        return [
            a
            for a in out
            if not (a.type == type_name and all(getattr(a, k) == v for k, v in match.items()))
        ]

    def _respond(kind: str) -> None:
        nonlocal out
        other = "reject" if kind == "accept" else "accept"
        out = _without("respond_to_offer", response=other)
        if not any(a.type == "respond_to_offer" for a in out):
            out.append(RespondToOfferAct(type="respond_to_offer", response=kind, quote=q))  # type: ignore[arg-type]

    # 1. accept / reject
    if _INJECTION_RE.search(utterance):
        out = _without("respond_to_offer", response="accept")
    elif _has_unguarded_reject_phrase(utterance):
        _respond("reject")
    elif _has_unguarded_accept_phrase(utterance) or (
        not has_terms and _ack_dominant(utterance)
    ):
        _respond("accept")

    # 2. read-back reply
    kept: list[Any] = []
    for a in out:
        if a.type == "readback_reply" and _verify_readback_response(a.response, utterance) is None:
            continue
        kept.append(a)
    out = kept

    # 3. private info / commitment
    private = any(a.type == "ask_private_info" for a in out) or repair_asks_client_private_info(
        False, utterance
    )
    if private and not any(a.type == "ask_private_info" for a in out):
        out.append(AskPrivateInfoAct(type="ask_private_info", quote=q))
    if not any(a.type == "demand_commitment" for a in out) and repair_demands_commitment(
        False, utterance
    ):
        out.append(DemandCommitmentAct(type="demand_commitment", quote=q))

    # 4. hostility
    score = max((HOSTILITY_LEVEL[a.level] for a in out if a.type == "hostility"), default=0.0)
    repaired = repair_hostility(score, utterance)
    out = _without("hostility")
    if repaired > 0:
        level = "strong" if repaired >= HOSTILITY_LEVEL["strong"] else "mild"
        out.append(HostilityAct(type="hostility", level=level, quote=q))  # type: ignore[arg-type]

    # 8 (part). "that's all": needed before the closing rule.
    list_done = any(a.type == "list_done" for a in out) or repair_list_done(utterance)
    if list_done and not any(a.type == "list_done" for a in out):
        out.append(ListDoneAct(type="list_done", quote=q))

    # 8 (part). Rule announcement, re-read by code.
    ann = next((a for a in out if a.type == "announce_rules"), None)
    field, count = repair_defines_field(
        ann.field if ann is not None else None,
        ann.count if ann is not None else None,
        utterance,
        given_fields=given_fields,
    )
    out = _without("announce_rules")
    levels: int | None = None
    if field == "max_segments" and count is not None:
        levels = count  # the level limit itself, not a list still to come
    elif field is not None:
        out.append(
            AnnounceRulesAct(
                type="announce_rules",
                field=field,  # type: ignore[arg-type]
                count=count,
                quote=ann.quote if ann is not None else q,
            )
        )
    collecting = collecting or field == "min_payment_tiers"

    # 5. closing
    said_list = any(a.type == "list_done" for a in acts)
    ends = model_ended or repair_wants_to_end(False, utterance)
    if list_done and (collecting or (said_list and not model_ended)):
        ends = False
    out = _without("end_call")
    if ends:
        kind = next((a for a in acts if a.type == "end_call"), None)
        out.append(kind or EndCallAct(type="end_call", quote=q))

    # 6. schedule detail
    if repair_asks_for_schedule(False, utterance) and not any(
        a.type == "question" and a.topic == "schedule" for a in out
    ):
        out.append(QuestionAct(type="question", topic="schedule", quote=q))

    # 7. firmness
    if not any(a.type == "price_ask" and a.firmness == "final" for a in out) and repair_firm(
        False, utterance
    ):
        out.append(PriceAskAct(type="price_ask", firmness="final", quote=q))

    # 8. off-script question (not the code's schedule topic)
    asked = [a for a in out if a.type == "question" and a.topic != "schedule"]
    topic = asked[0].topic if asked else None
    asks, topic = repair_question(bool(asked), topic, utterance, asks_private=private)
    out = [a for a in out if not (a.type == "question" and a.topic != "schedule")]
    if asks:
        out.append(
            QuestionAct(
                type="question",
                topic=topic or "other",  # type: ignore[arg-type]
                quote=asked[0].quote if asked else q,
            )
        )
    return out, levels
