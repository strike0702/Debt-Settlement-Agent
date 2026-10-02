"""Pull money / pct / date / count figures out of agent or creditor text.

Used by ``guards.rendered_guard`` (and later NLU checks). This is *not*
spoken-unit formatting — that lives in ``app.domain.units``. Here we only:

1. Find figure spans in a fixed priority order (money ``$…``, abbrev ``2.5k``,
   percentages, dates, ordinals, bare digits, English number-words).
2. Mask each matched span so a later pass cannot re-parse the same digits.
3. Normalize each hit to ``(kind, value)`` with money in integer cents and
   percentages in integer basis points.

``NUMBER_WORDS`` / ``ONE_ALLOWLIST_PHRASES`` are also shared with
``template_guard``, which forbids number-words in unfilled templates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

TokenKind = Literal["money", "pct", "count", "date", "ordinal"]

# Forbidden in templates; also the lexicon for word-phrase extraction.
NUMBER_WORDS: frozenset[str] = frozenset(
    {
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
        "million",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "half",
        "quarter",
        "dozen",
        "couple",
    }
)

# template_guard: bare "one" blocks; these idioms are fine.
ONE_ALLOWLIST_PHRASES: tuple[str, ...] = (
    "no one",
    "one moment",
    "one more",
    "one second",
)

_ONES: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}

_TENS: dict[str, int] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

_ORDINALS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
}

_SCALES: dict[str, int] = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
}

_FRACTIONS: dict[str, int] = {
    "half": 1,  # treated as count-ish sentinel; callers rarely need exact
    "quarter": 1,
    "dozen": 12,
    "couple": 2,
}

_MONTHS: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# Comma-grouped form first (requires at least one comma group), then plain digits.
_MONEY_RE = re.compile(
    r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\$\s?\d+(?:\.\d{2})?",
)
_PCT_RE = re.compile(r"\d+(?:\.\d+)?\s?(?:%|percent)", re.IGNORECASE)
_DATE_NAMED_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s*(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)
_DATE_ISO_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_DATE_SLASH_RE = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\b",
)
_ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE)
_ABBREV_RE = re.compile(r"\b\d+(?:\.\d+)?[kKmM]\b")
_BARE_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\b")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)?")


@dataclass(frozen=True)
class NumberToken:
    """One extracted figure: typed value plus the raw span in the source text."""

    kind: TokenKind
    value: int | date
    raw: str
    start: int
    end: int

    def as_pair(self) -> tuple[str, int | date]:
        return (self.kind, self.value)


def words_to_number(text: str) -> int | None:
    """Convert common English number phrases to an int.

    Handles forms like ``two hundred fifty``, ``twenty-five``, and
    ``twenty-five hundred``. Returns None if the phrase is not a number.
    """
    raw = text.strip().lower().replace("-", " ")
    if not raw:
        return None
    parts = [p for p in raw.split() if p]
    if not parts:
        return None

    # Single-token ordinals / fractions / dozen / couple.
    if len(parts) == 1 and parts[0] in _ORDINALS:
        return _ORDINALS[parts[0]]
    if len(parts) == 1 and parts[0] in _FRACTIONS:
        return _FRACTIONS[parts[0]]

    total = 0
    current = 0
    saw_numeric = False
    for part in parts:
        if part in _ONES:
            current += _ONES[part]
            saw_numeric = True
        elif part in _TENS:
            current += _TENS[part]
            saw_numeric = True
        elif part in _ORDINALS:
            current += _ORDINALS[part]
            saw_numeric = True
        elif part == "hundred":
            if current == 0:
                current = 1
            current *= 100
            saw_numeric = True
        elif part in ("thousand", "million"):
            if current == 0:
                current = 1
            total += current * _SCALES[part]
            current = 0
            saw_numeric = True
        elif part in _FRACTIONS:
            # "a dozen" / lone fraction words — only valid alone (handled above)
            return None
        else:
            return None
    if not saw_numeric:
        return None
    return total + current


def _mask_span(masked: list[bool], start: int, end: int) -> None:
    for i in range(start, end):
        masked[i] = True


def _span_free(masked: list[bool], start: int, end: int) -> bool:
    return not any(masked[start:end])


def _parse_money_raw(raw: str) -> int:
    s = raw.strip().replace(",", "").replace("$", "").strip()
    if "." in s:
        dollars, cents = s.split(".", 1)
        return int(dollars or "0") * 100 + int(cents.ljust(2, "0")[:2])
    return int(s) * 100


def _parse_pct_raw(raw: str) -> int:
    s = raw.strip().lower().replace("%", "").replace("percent", "").strip()
    from decimal import Decimal

    return int(Decimal(s) * 100)


def _parse_abbrev(raw: str) -> tuple[TokenKind, int]:
    """``2.5k`` / ``10K`` / ``1.2m`` → money cents (k=thousand, m=million dollars)."""
    from decimal import Decimal

    s = raw.strip()
    suffix = s[-1].lower()
    num = Decimal(s[:-1])
    if suffix == "k":
        dollars = num * Decimal(1_000)
    else:
        dollars = num * Decimal(1_000_000)
    return ("money", int((dollars * Decimal(100)).to_integral_value()))


def _parse_bare(raw: str) -> tuple[TokenKind, int]:
    s = raw.strip().replace(",", "")
    if "." in s:
        # Decimal dollars without $ — treat as money cents.
        from decimal import Decimal

        return ("money", int(Decimal(s) * 100))
    return ("count", int(s))


def _year_from(y: str | None, ref: date) -> int:
    if y is None:
        return ref.year
    yi = int(y)
    if yi < 100:
        return 2000 + yi
    return yi


def extract_tokens(text: str, *, ref: date | None = None) -> list[NumberToken]:
    """Extract figures in PLAN §6.4 order; each match masks its character span.

    ``ref`` supplies the year when a date has none (e.g. ``April 15``).
    """
    if ref is None:
        ref = date.today()
    masked = [False] * len(text)
    tokens: list[NumberToken] = []

    def add(kind: TokenKind, value: int | date, raw: str, start: int, end: int) -> None:
        if not _span_free(masked, start, end):
            return
        tokens.append(NumberToken(kind=kind, value=value, raw=raw, start=start, end=end))
        _mask_span(masked, start, end)

    # 1. Money with $
    for m in _MONEY_RE.finditer(text):
        add("money", _parse_money_raw(m.group(0)), m.group(0), m.start(), m.end())

    # 1b. Hidden abbrev money (2.5k) — before bare digits can claim "2.5".
    for m in _ABBREV_RE.finditer(text):
        kind, value = _parse_abbrev(m.group(0))
        add(kind, value, m.group(0), m.start(), m.end())

    # 2. Percentages
    for m in _PCT_RE.finditer(text):
        add("pct", _parse_pct_raw(m.group(0)), m.group(0), m.start(), m.end())

    # 3. Dates (named, ISO, slash) — skip invalid calendars (no crash).
    for m in _DATE_NAMED_RE.finditer(text):
        month = _MONTHS[m.group("month").lower()]
        day = int(m.group("day"))
        year = _year_from(m.group("year"), ref)
        try:
            add("date", date(year, month, day), m.group(0), m.start(), m.end())
        except ValueError:
            continue

    for m in _DATE_ISO_RE.finditer(text):
        try:
            add(
                "date",
                date(int(m.group("year")), int(m.group("month")), int(m.group("day"))),
                m.group(0),
                m.start(),
                m.end(),
            )
        except ValueError:
            continue

    for m in _DATE_SLASH_RE.finditer(text):
        month = int(m.group("month"))
        day = int(m.group("day"))
        year = _year_from(m.group("year"), ref)
        try:
            add("date", date(year, month, day), m.group(0), m.start(), m.end())
        except ValueError:
            continue

    # 4. Ordinals
    for m in _ORDINAL_RE.finditer(text):
        raw = m.group(0)
        n = int(re.match(r"\d+", raw).group(0))  # type: ignore[union-attr]
        add("ordinal", n, raw, m.start(), m.end())

    # 5. Bare numbers (including comma-grouped and decimals without $)
    for m in _BARE_RE.finditer(text):
        kind, value = _parse_bare(m.group(0))
        add(kind, value, m.group(0), m.start(), m.end())

    # 6. Number words (greedy multi-word phrases)
    # Scan word tokens; try longest phrase of NUMBER_WORDS.
    word_matches = list(_WORD_TOKEN_RE.finditer(text))
    i = 0
    while i < len(word_matches):
        wm = word_matches[i]
        if not _span_free(masked, wm.start(), wm.end()):
            i += 1
            continue
        # Expand hyphenated pieces into word list for classification.
        pieces = wm.group(0).lower().replace("-", " ").split()
        if not pieces or pieces[0] not in NUMBER_WORDS:
            i += 1
            continue

        # Greedily take following number-word tokens.
        j = i + 1
        words = list(pieces)
        end = wm.end()
        while j < len(word_matches):
            nxt = word_matches[j]
            # Only glue if contiguous whitespace between.
            gap = text[end : nxt.start()]
            if gap.strip() != "" or not _span_free(masked, nxt.start(), nxt.end()):
                break
            nxt_pieces = nxt.group(0).lower().replace("-", " ").split()
            if not nxt_pieces or any(p not in NUMBER_WORDS for p in nxt_pieces):
                break
            words.extend(nxt_pieces)
            end = nxt.end()
            j += 1

        phrase = " ".join(words)
        value = words_to_number(phrase)
        if value is None:
            i += 1
            continue

        # Ordinal words → ordinal kind; else count (money inferred by guards via dollars).
        kind: TokenKind = "ordinal" if len(words) == 1 and words[0] in _ORDINALS else "count"
        # Special: "... dollars" after number words → money (handled by caller context);
        # detect trailing "dollars"/"dollar" as money conversion without consuming as number word.
        raw = text[wm.start() : end]
        money_tail = re.match(r"\s+dollars?\b", text[end:], re.IGNORECASE)
        if money_tail:
            end_ext = end + money_tail.end()
            add("money", value * 100, text[wm.start() : end_ext], wm.start(), end_ext)
        else:
            add(kind, value, raw, wm.start(), end)
        i = j

    tokens.sort(key=lambda t: t.start)
    return tokens


def normalize_token(token: NumberToken) -> tuple[str, int | date]:
    """Normalize a token to ``(kind, value)`` for guard matching."""
    return token.as_pair()
