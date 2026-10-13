"""NLU answers that skip the LLM: replies the agent can read with code alone.

``try_fast_readback`` reads a plain yes / no to a pending read-back;
``try_fast_field_answer`` reads a bare answer ("six", "$250") to the field
question the agent just asked; ``try_resolve_cents_clarify`` reads the reply
to "dollars or cents?"; ``try_resolve_disputed_value`` reads which side of
"$180 or $250?" the rep picked for a CONTRADICTED term ("$250", "the new
one"). Each returns ``None`` when the line is anything more,
so ``llm_call.analyze`` falls through to the model. Value checks reuse
``verify``'s helpers so a fast answer is held to the same bar.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.agent.nlu.models import VerifiedAnalysis, VerifiedTerm
from app.agent.nlu.quotes import (
    _in_prior_range,
    _is_bare_digit_money,
    _utterance_has_money_unit,
    quote_in_utterance,
)
from app.agent.numbers import extract_tokens
from app.domain.fields import FIELD_REGISTRY, FIELDS_BY_NAME

_PLAIN_YES_RE = re.compile(
    r"^\s*(?:yes|yep|yeah|correct|that's right|thats right|that is correct|"
    r"confirmed|confirm|right)\s*[.!]?\s*$",
    re.I,
)
_PLAIN_NO_RE = re.compile(
    r"^\s*(?:no|nope|incorrect|that's wrong|thats wrong|not correct|deny)\s*[.!]?\s*$",
    re.I,
)


def try_fast_readback(utterance: str, pending_readback: str | None) -> VerifiedAnalysis | None:
    """Skip the LLM when pending read-back and the utterance is plain yes/no."""
    if not pending_readback:
        return None
    text = utterance.strip()
    if _PLAIN_YES_RE.match(text):
        return VerifiedAnalysis(
            stance="info",
            readback_response="confirm",
        )
    if _PLAIN_NO_RE.match(text):
        return VerifiedAnalysis(
            stance="info",
            readback_response="deny",
        )
    return None


def _field_for_ask_line(last_agent_line: str) -> str | None:
    """Return the registry field when the agent last asked that field's question."""
    line = (last_agent_line or "").strip()
    if not line:
        return None
    for spec in FIELD_REGISTRY:
        if line == spec.ask_text or spec.ask_text in line:
            return spec.name
    return None
_CENTS_CLARIFY_DOLLARS_RE = re.compile(
    r"(?:"
    r"\bdollars?\b"
    r"|\bbucks?\b"
    r"|\bfirst\b"
    r"|\bthe first\b"
    r"|\$"
    r")",
    re.IGNORECASE,
)
_CENTS_CLARIFY_CENTS_RE = re.compile(
    r"(?:"
    r"\bcents?\b"
    r"|\bsecond\b"
    r"|\bthe second\b"
    r")",
    re.IGNORECASE,
)


def try_fast_field_answer(
    utterance: str,
    last_agent_line: str,
    *,
    ref: date | None = None,
) -> VerifiedAnalysis | None:
    """Skip the LLM for a bare numeric/enum reply to the agent's last ASK.

    Bare amounts for cents fields (no ``$`` / dollars / cents) are ambiguous —
    return ``cents_ambiguity_*`` so policy can ask which unit.
    """
    field = _field_for_ask_line(last_agent_line)
    if field is None:
        return None
    spec = FIELDS_BY_NAME[field]
    text = utterance.strip()
    if not text:
        return None
    # Only bare / near-bare answers — leave "about two-fifty a month" to the LLM.
    if len(text) > 40 or len(text.split()) > 4:
        return None
    ref_d = ref or date.today()
    tokens = extract_tokens(text, ref=ref_d)

    value: Any = None
    quote = text
    if spec.kind == "cents":
        money = [t for t in tokens if t.kind == "money"]
        counts = [t for t in tokens if t.kind == "count"]
        if len(money) == 1:
            value = money[0].value
            quote = money[0].raw
        elif len(counts) == 1 and not money:
            bare = counts[0].value
            quote = counts[0].raw
            if _is_bare_digit_money(quote, text):
                return VerifiedAnalysis(
                    stance="info",
                    cents_ambiguity_bare=bare,
                    cents_ambiguity_field=field,
                )
            if _utterance_has_money_unit(text):
                # "110 dollars" without a money-token — treat as dollars.
                value = bare * 100
            else:
                # Number-words without a unit — let the LLM handle it.
                return None
        else:
            return None
    elif spec.kind == "int":
        counts = [t for t in tokens if t.kind in ("count", "ordinal")]
        if len(counts) != 1:
            return None
        value = counts[0].value
        quote = counts[0].raw
    elif spec.kind == "enum":
        # Word-boundary only — "evening" must not match "even".
        for opt in spec.prior_range or ():
            if quote_in_utterance(str(opt), text):
                value = opt
                quote = str(opt)
                break
        if value is None:
            return None
    else:
        return None

    if not _in_prior_range(field, value):
        return None
    return VerifiedAnalysis(
        terms=[
            VerifiedTerm(
                field=field,
                value=value,
                quote=quote,
                hedged=False,
                verified=True,
            )
        ],
        stance="info",
    )


def try_resolve_cents_clarify(
    utterance: str,
    pending: dict[str, Any],
    *,
    ref: date | None = None,
) -> VerifiedAnalysis | None:
    """Resolve a pending dollars-vs-cents clarify into a verified cents term."""
    field = str(pending.get("field") or "")
    if field not in FIELDS_BY_NAME or FIELDS_BY_NAME[field].kind != "cents":
        return None
    as_dollars = int(pending["as_dollars"])
    as_cents = int(pending["as_cents"])
    ref_d = ref or date.today()
    text = utterance.strip()
    chosen: int | None = None
    quote = text

    if _CENTS_CLARIFY_DOLLARS_RE.search(text) and not _CENTS_CLARIFY_CENTS_RE.search(
        text
    ):
        chosen = as_dollars
    elif _CENTS_CLARIFY_CENTS_RE.search(text) and not _CENTS_CLARIFY_DOLLARS_RE.search(
        text
    ):
        chosen = as_cents
    else:
        for tok in extract_tokens(text, ref=ref_d):
            if tok.kind == "money" and tok.value in (as_dollars, as_cents):
                chosen = int(tok.value)
                quote = tok.raw
                break
            if tok.kind == "count" and tok.value * 100 == as_dollars:
                # Restated bare → prefer dollars reading once clarifying.
                if _utterance_has_money_unit(text) and "cent" in text.lower():
                    chosen = as_cents
                elif _utterance_has_money_unit(text):
                    chosen = as_dollars
                break

    if chosen is None and _PLAIN_YES_RE.match(text):
        # "yes" after "is that $110 or $1.10?" is too vague — require an option.
        return None

    if chosen is None:
        return None
    if not _in_prior_range(field, chosen):
        # Picked an out-of-range reading — clear pending via empty resolve marker.
        return VerifiedAnalysis(
            stance="info",
            cents_ambiguity_bare=None,
            cents_ambiguity_field=None,
            terms=[],
        )
    return VerifiedAnalysis(
        terms=[
            VerifiedTerm(
                field=field,
                value=chosen,
                quote=quote,
                hedged=False,
                verified=True,
            )
        ],
        stance="info",
    )


# Which side of an open "which is it, X or Y?" the rep picked, without a value.
# "first" / "earlier" alone can name a date ("the first", "an earlier date"),
# so date fields take only the words below that cannot.
_PICK_OLD_RE = re.compile(
    r"\b(?:the\s+)?(?:first|earlier|old|older|original|former|previous)(?:\s+one)?\b",
    re.IGNORECASE,
)
_PICK_NEW_RE = re.compile(
    r"\b(?:the\s+)?(?:second|new|newer|latter|latest|updated|last)(?:\s+one)?\b",
    re.IGNORECASE,
)
_PICK_DATE_SAFE_RE = re.compile(
    r"\b(?:(?:first|second|earlier|last)\s+one|old|older|original|former|previous"
    r"|new|newer|latter|latest|updated)\b",
    re.IGNORECASE,
)
# A negated or hedged pick is not an answer ("not $250", "maybe the first").
_PICK_VETO_RE = re.compile(
    r"\b(?:not|never|neither|nor|\w+n['’]t|maybe|probably|perhaps|about|around"
    r"|roughly|approximately|think|guess|either)\b",
    re.IGNORECASE,
)


def try_resolve_disputed_value(
    utterance: str,
    field: str,
    old: Any,
    new: Any,
    *,
    ref: date | None = None,
) -> VerifiedAnalysis | None:
    """Read a short reply to "which is it, ``old`` or ``new``?" for a CONTRADICTED field.

    The reply must fit the fast-path size limits and either name exactly one
    of the two values (money with ``$`` / dollars for cents fields, a count
    for count fields, a date for dates) or say "the first / earlier / old one"
    vs "the second / new / latter one". Returns a verified, unhedged term for
    the picked value, else None (a third value, both, neither, a negated or
    hedged pick, or anything longer goes the usual way).
    """
    if field not in FIELDS_BY_NAME or old is None or new is None or old == new:
        return None
    kind = FIELDS_BY_NAME[field].kind
    if kind not in ("cents", "int", "date"):
        return None
    text = utterance.strip()
    if not text or len(text) > 40 or len(text.split()) > 4:
        return None
    if _PICK_VETO_RE.search(text):
        return None

    old_hit = _PICK_OLD_RE.search(text)
    new_hit = _PICK_NEW_RE.search(text)
    if bool(old_hit) != bool(new_hit):
        hit = old_hit or new_hit
        assert hit is not None
        if kind == "date" and not _PICK_DATE_SAFE_RE.search(hit.group()):
            return None
        # The pick words parse as numbers ("first one" → 2); nothing else may.
        rest = text[: hit.start()] + " " + text[hit.end() :]
        if extract_tokens(rest, ref=ref or date.today()):
            return None
        return _picked(field, old if old_hit else new, hit.group())

    tokens = extract_tokens(text, ref=ref or date.today())
    if kind == "cents":
        wanted = [t for t in tokens if t.kind == "money"]
        values = [int(t.value) for t in wanted]
        if not wanted and _utterance_has_money_unit(text):
            # "250 dollars" parses as a count next to a unit word.
            wanted = [t for t in tokens if t.kind == "count"]
            values = [int(t.value) * 100 for t in wanted]
    elif kind == "int":
        wanted = [t for t in tokens if t.kind == "count"]
        values = [int(t.value) for t in wanted]
    else:
        wanted = [t for t in tokens if t.kind == "date"]
        values = [t.value for t in wanted]
    if len(tokens) != 1 or len(wanted) != 1 or values[0] not in (old, new):
        return None
    return _picked(field, values[0], wanted[0].raw)


def _picked(field: str, value: Any, quote: str) -> VerifiedAnalysis:
    return VerifiedAnalysis(
        terms=[VerifiedTerm(field=field, value=value, quote=quote, hedged=False, verified=True)],
        stance="info",
    )
