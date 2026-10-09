"""Figure and stance check for the sim's LLM rewrite (Phase 46a).

``CreditorPolicy._phrase`` lets the ``sim`` LLM restyle a code-built draft.
Free models invent or change numbers ("the 10% balance", "we cannot go below
21%") and contradict themselves ("We cannot accept 48%, but we will accept
48%"). ``rewrite_problem(draft, rewrite, stance=...)`` names the first reason
to throw the rewrite away and speak the draft instead, or ``None``. Phase 49
adds the self-contradictions the Phase 46c re-check let through: a trailing
"Actually, it isn't.", a leading "No," on an info line, and a "Sure, ..."
opener on a refusal (all ``stance``).

This is a small, self-contained figure finder for sim lines, not the agent's
guard extractor (``app.agent.numbers``, which ``sim/`` must not import). Both
texts go through the same finder, so the check only has to be consistent:
money (``$346.80``, "300 dollars"), percentages ("48%", "forty-eight
percent"), dates ("March 31st"; the year is ignored) and any other number,
digits or words ("six payments", "3rd"). It is deliberately conservative:
an unsure match falls back to the draft, which is always correct.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

FallbackReason = Literal["figures", "private", "stance", "commit", "empty", "error"]

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}  # fmt: skip
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)  # fmt: skip

# "first" / "second" are mostly style ("the first payment", "one second"); the
# sim renders positions as digits ("2nd"), so they are not counted as figures.
_STYLE_ORDINALS = frozenset({"first", "second"})
_NUM_WORD = "|".join(
    sorted(
        [*_UNITS, *_TENS, *(o for o in _ORDINALS if o not in _STYLE_ORDINALS), *_SCALES],
        key=len,
        reverse=True,
    )
)
_WORDS_RE = rf"(?:{_NUM_WORD})(?:(?:[\s-]+|\s+and\s+)(?:{_NUM_WORD}))*"
_DIGITS_RE = r"\d[\d,]*(?:\.\d+)?"

_MONEY_RE = re.compile(
    rf"\$\s?(?P<d>{_DIGITS_RE})(?:\s*(?P<k>k|thousand)\b)?"
    rf"|\b(?P<dw>{_DIGITS_RE}|{_WORDS_RE})\s+dollars?\b",
    re.IGNORECASE,
)
_PCT_RE = re.compile(
    rf"(?P<n>{_DIGITS_RE}|\b{_WORDS_RE})\s*(?:%|\s+percent\b|\s+per\s+cent\b)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    rf"\b(?P<m>{'|'.join(_MONTHS)})\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b"
    rf"(?:,?\s+(?:19|20)\d{{2}}\b)?"
    rf"|\b(?:the\s+)?(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\s+of\s+(?P<m2>{'|'.join(_MONTHS)})\b"
    rf"(?:,?\s+(?:19|20)\d{{2}}\b)?",
    re.IGNORECASE,
)
_NUM_RE = re.compile(rf"\b(?P<n>{_DIGITS_RE})(?:st|nd|rd|th)?\b|\b(?P<w>{_WORDS_RE})\b", re.I)
# "one" is a pronoun more often than a number ("that one works"); count it
# only before a unit noun.
_ONE_UNIT_RE = re.compile(r"\s*(?:payments?|percent|dollars?|levels?|tiers?|tokens?)\b", re.I)

Figure = tuple[str, object]

# Client-private wording the sim must never put in the rep's mouth unprompted.
_PRIVATE_RE = re.compile(
    r"\b(?:income|take[- ]home|monthly budget|budget|bank balance|savings|salary|"
    r"paycheck|wages?|net pay|disposable|account balance|checking account)\b",
    re.IGNORECASE,
)

# Stance markers. A phrase counts only when no negator sits shortly before it
# in the same clause ("we cannot accept" is not an accept).
_ACCEPT_RE = re.compile(
    r"\b(?:agreed|agree|accept(?:ed|able)?|works for us|that works|sounds good|"
    r"we can do|we have a deal|it's a deal|yes|approved?)\b",
    re.IGNORECASE,
)
_REJECT_RE = re.compile(
    r"\b(?:no|not|cannot|can't|can ?not|won't|unable|doesn't work|does not work|"
    r"too low|too high|decline|reject(?:ed)?|unfortunately|incorrect|wrong)\b",
    re.IGNORECASE,
)
_NEGATOR_RE = re.compile(
    r"\b(?:not|no|never|cannot|can't|can ?not|won't|unable|don't|doesn't|isn't)\b",
    re.IGNORECASE,
)
_IDIOM_RE = re.compile(r"\b(?:no problem|no worries|not a problem|no doubt)\b", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"[.;,:!?—]|\bbut\b|\bhowever\b|\bthough\b", re.IGNORECASE)

# Phase 49 (ledger 46c.2 / 46c.5): shapes that flip a line without adding a
# phrase from the lists above. A short sentence that only takes back what came
# before ("Actually, it isn't.", "Actually, we can't."); a leading "No," on a
# line that is not a refusal (the agent reads it as a correction); and an
# accept-sounding opener on a refusal ("Sure, we can't move on that.").
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]*")
_SELF_NEGATION_RE = re.compile(
    r"^\W*(?:(?:actually|wait|oh|well|sorry|hmm|or)\W+)*"
    r"(?:no|nope|nah|not really|scratch that|never ?mind"
    r"|(?:it|that|this|they|we|i)(?:'?s|'re|'m)?\s+"
    r"(?:is |are |am |was |were |do |does |did |can |will )?"
    r"(?:not|n't|isn't|aren't|wasn't|weren't|can't|cannot|can not|won't|don't|doesn't|didn't)"
    r"(?:\s+(?:actually|really|true|right|so|do that|do it|go there|anymore|any more))?"
    r"|that'?s not (?:right|true|it|correct))\W*$",
    re.IGNORECASE,
)
_LEADING_NO_RE = re.compile(r"^\W*(?:no|nope|nah)\b(?!\s+(?:problem|worries|doubt))", re.I)
_ACCEPT_OPENER_RE = re.compile(
    r"^\W*(?:sure|yes|yeah|yep|ok(?:ay)?|absolutely|of course|great|perfect|definitely"
    r"|certainly|alright|all right|happy to|no problem)\b",
    re.IGNORECASE,
)

# Drafts that state these stances must not be rewritten into the other one.
_ACCEPT_STANCES = frozenset({"accept", "confirm"})
_REJECT_STANCES = frozenset({"reject", "deny"})


def _words_value(text: str) -> int | None:
    """``forty-eight`` → 48, ``one hundred twenty`` → 120, ``third`` → 3."""
    total = cur = 0
    seen = False
    for w in re.split(r"[\s-]+", text.lower()):
        if w in ("", "and"):
            continue
        seen = True
        if w in _UNITS:
            cur += _UNITS[w]
        elif w in _TENS:
            cur += _TENS[w]
        elif w in _ORDINALS:
            cur += _ORDINALS[w]
        elif w == "hundred":
            cur = max(cur, 1) * 100
        elif w in _SCALES:
            total += max(cur, 1) * _SCALES[w]
            cur = 0
        else:
            return None
    return total + cur if seen else None


def _number(raw: str) -> Decimal | None:
    raw = raw.strip()
    if raw and raw[0].isdigit():
        try:
            return Decimal(raw.replace(",", ""))
        except InvalidOperation:
            return None
    v = _words_value(raw)
    return Decimal(v) if v is not None else None


def figures(text: str) -> set[Figure]:
    """Every figure in ``text`` as ``(kind, value)``: money in cents, pct in bp, dates, numbers."""
    out: set[Figure] = set()
    masked = list(text)

    def take(m: re.Match[str]) -> None:
        for i in range(m.start(), m.end()):
            masked[i] = " "

    for m in _MONEY_RE.finditer(text):
        n = _number(m.group("d") or m.group("dw") or "")
        if n is None:
            continue
        if m.group("k"):
            n *= 1000
        out.add(("money", int(n * 100)))
        take(m)
    rest = "".join(masked)
    for m in _PCT_RE.finditer(rest):
        n = _number(m.group("n"))
        if n is not None:
            out.add(("pct", int(n * 100)))
            take(m)
    rest = "".join(masked)
    for m in _DATE_RE.finditer(rest):
        month = (m.group("m") or m.group("m2")).lower()
        day = int(m.group("d") or m.group("d2"))
        out.add(("date", (_MONTHS.index(month) + 1, day)))
        take(m)
    rest = "".join(masked)
    for m in _NUM_RE.finditer(rest):
        raw = m.group("n") or m.group("w")
        if raw.lower() == "one" and not _ONE_UNIT_RE.match(rest, m.end()):
            continue
        n = _number(raw)
        if n is not None:
            out.add(("num", n.normalize() if n % 1 else int(n)))
    return out


def _unnegated(pattern: re.Pattern[str], text: str) -> set[str]:
    """Phrases of ``pattern`` with no negator earlier in their clause."""
    clean = _IDIOM_RE.sub(" ", text)
    found: set[str] = set()
    for m in pattern.finditer(clean):
        head = clean[: m.start()]
        parts = _CLAUSE_RE.split(head)
        clause = parts[-1] if parts else ""
        if not _NEGATOR_RE.search(clause):
            found.add(m.group(0).lower())
    return found


def _rejects(text: str) -> set[str]:
    return {m.group(0).lower() for m in _REJECT_RE.finditer(_IDIOM_RE.sub(" ", text))}


def _self_negations(text: str) -> int:
    """Sentences that only take back what came before ("Actually, we can't.")."""
    return sum(1 for m in _SENTENCE_RE.finditer(text) if _SELF_NEGATION_RE.match(m.group(0)))


def stance_flipped(draft: str, rewrite: str, stance: str | None) -> bool:
    """True when ``rewrite`` adds a marker of the opposite stance to ``draft``'s.

    ``stance`` is the draft's oracle stance (or read-back answer). An accept
    draft must not gain a rejection phrase; a reject draft must not gain an
    unnegated accept phrase or an accept-sounding opener ("Sure, ..."). Other
    stances also fail when the rewrite adds both an accept and a reject phrase
    the draft did not have, a leading "No,", or a sentence that takes the line
    back ("Actually, it isn't.").
    """
    if stance in _REJECT_STANCES:
        # A refusal may say "No." twice; it must not open like a yes.
        if _ACCEPT_OPENER_RE.match(rewrite) and not _ACCEPT_OPENER_RE.match(draft):
            return True
    else:
        if _self_negations(rewrite) > _self_negations(draft):
            return True
        if _LEADING_NO_RE.match(rewrite) and not _LEADING_NO_RE.match(draft):
            return True
    new_accept = _unnegated(_ACCEPT_RE, rewrite) - _unnegated(_ACCEPT_RE, draft)
    new_reject = _rejects(rewrite) - _rejects(draft)
    if stance in _ACCEPT_STANCES:
        return bool(new_reject)
    if stance in _REJECT_STANCES:
        return bool(new_accept)
    return bool(new_accept and new_reject)


def rewrite_problem(draft: str, rewrite: str, *, stance: str | None) -> FallbackReason | None:
    """First reason to drop ``rewrite`` for ``draft``, else ``None`` (rewrite is safe)."""
    if not rewrite.strip():
        return "empty"
    if figures(rewrite) != figures(draft):
        return "figures"
    if _PRIVATE_RE.search(rewrite) and not _PRIVATE_RE.search(draft):
        return "private"
    if stance_flipped(draft, rewrite, stance):
        return "stance"
    return None
