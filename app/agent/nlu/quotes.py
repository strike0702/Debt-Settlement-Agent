"""Quote and value primitives shared by every NLU check.

A term is only trusted when its quote is the rep's own words:
``quote_in_utterance`` matches whole words after ``normalize_for_quote``
(so "six" never matches inside "sixteen"). ``_in_prior_range`` bounds a
value by its field's registry range, and the money-unit helpers tell "$250"
from a bare "250". Pure functions; used by ``verify``, ``fast_paths``,
``cues`` and ``amounts``.
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.fields import FIELDS_BY_NAME


def normalize_for_quote(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (quote match)."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def quote_in_utterance(quote: str, utterance: str) -> bool:
    """True when normalized quote appears as whole word(s) in the utterance.

    Uses word boundaries so ``six`` does not match inside ``sixteen`` and
    ``250`` does not match inside ``1250``.
    """
    q = normalize_for_quote(quote)
    if not q:
        return False
    u = normalize_for_quote(utterance)
    return bool(re.search(rf"(?<!\w){re.escape(q)}(?!\w)", u))


_MONEY_UNIT_RE = re.compile(
    r"(?:\$|\bdollars?\b|\bcents?\b|\bbucks?\b)",
    re.IGNORECASE,
)
_BARE_DIGIT_QUOTE_RE = re.compile(r"^\d[\d,]*$")


def _utterance_has_money_unit(text: str) -> bool:
    return bool(_MONEY_UNIT_RE.search(text))


def _is_bare_digit_money(quote: str, utterance: str) -> bool:
    """True when the amount is only digits — no $ / dollars / cents / number-words."""
    if _utterance_has_money_unit(quote) or _utterance_has_money_unit(utterance):
        return False
    return bool(_BARE_DIGIT_QUOTE_RE.match(quote.strip()))


def _in_prior_range(field: str, value: Any) -> bool:
    spec = FIELDS_BY_NAME.get(field)
    if spec is None or spec.prior_range is None:
        return True
    pr = spec.prior_range
    if spec.kind == "enum":
        return value in pr
    if (
        len(pr) == 2
        and isinstance(pr[0], int)
        and isinstance(pr[1], int)
        and isinstance(value, int)
    ):
        return int(pr[0]) <= value <= int(pr[1])
    return True
