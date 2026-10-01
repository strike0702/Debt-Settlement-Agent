"""NLG safety layer: block bad templates and unsafe spoken sentences.

Policy decides *what* to say; NLG fills placeholders from PUBLIC ``Fact``s.
These guards are the deterministic backstop so the LLM cannot:

- put raw digits / ``$`` / ``%`` / number-words into a template
  (``template_guard`` — figures must stay as ``{fact_id}`` placeholders)
- speak a figure that is not a PUBLIC fact or a number the creditor said
  (``rendered_guard`` → ``unverified_number``)
- leak a PRIVATE value in any rendering (``boundary``)
- prematurely commit ("we agree", "it's a deal", …) (``commitment``)

Pipeline (later phases): LLM/template → ``template_guard`` → ``Fact.render``
→ ``rendered_guard`` → speak or ``SAFE_FALLBACK``. Token digging lives in
``numbers.py``; this module only decides pass/block.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from pydantic import BaseModel, Field

from app.agent.numbers import (
    NUMBER_WORDS,
    ONE_ALLOWLIST_PHRASES,
    NumberToken,
    extract_tokens,
)
from app.domain.facts import Fact, FactSet

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_DIGIT_RE = re.compile(r"\d")
# Premature deal language — agent must not lock terms before policy says so.
_COMMITMENT_RE = re.compile(
    r"\b(?:we|i|my client|the client)\s+"
    r"(?:agrees?|accepts?|commits?|guarantees?|promises?)\b"
    r"|\bit'?s a deal\b"
    r"|\bwe have a deal\b"
    r"|\bsounds like a deal\b"
    r"|\ba deal\b"
    r"|\bdeal\b"
    r"|\bagreed\b"
    r"|\bwe have an agreement\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"\b[A-Za-z]+(?:-[A-Za-z]+)*\b")


class GuardResult(BaseModel):
    """Outcome of a guard check.

    ``reason`` is empty on pass. On block it is one of:
    template — ``digit``, ``dollar``, ``percent``, ``number_word``,
    ``unknown_placeholder``, ``missing_required``;
    rendered — ``unverified_number``, ``boundary``, ``commitment``.
    """

    ok: bool
    reason: str = ""
    offending: list[str] = Field(default_factory=list)


def _fact_pairs(public_facts: FactSet | dict[str, Fact]) -> set[tuple[str, int | date]]:
    facts: Iterable[Fact]
    if isinstance(public_facts, FactSet):
        facts = public_facts.public().values()
    else:
        facts = public_facts.values()
    return {(f.kind, f.value) for f in facts}


def _cross_match(
    kind: str,
    value: int | date,
    allowed: set[tuple[str, int | date]],
) -> bool:
    """Match (kind, value), with money↔count dollar equivalence."""
    if (kind, value) in allowed:
        return True
    if kind == "ordinal":
        if ("count", value) in allowed:
            return True
        if ("ordinal", value) in allowed:
            return True
    if kind == "count" and isinstance(value, int):
        if ("ordinal", value) in allowed:
            return True
        if ("money", value * 100) in allowed:
            return True
    if kind == "money" and isinstance(value, int) and value % 100 == 0:
        if ("count", value // 100) in allowed:
            return True
    return False


def _mask_allowlist(text: str) -> str:
    """Replace allowlisted 'one' phrases so leftover number-word scans skip them."""
    out = text
    for phrase in ONE_ALLOWLIST_PHRASES:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        out = pattern.sub(lambda m: " " * len(m.group(0)), out)
    return out


def template_guard(
    text: str,
    allowed_ids: set[str] | frozenset[str],
    required_ids: set[str] | frozenset[str],
) -> GuardResult:
    """Validate an unfilled NLG template (placeholders only, no spoken figures).

    Checks, in order: digits, ``$``, ``%``, forbidden number-words (with the
    ``one`` allowlist phrases blanked out), then placeholder id set vs
    ``allowed_ids`` / ``required_ids``.
    """
    if _DIGIT_RE.search(text):
        m = _DIGIT_RE.search(text)
        assert m is not None
        return GuardResult(ok=False, reason="digit", offending=[m.group(0)])

    if "$" in text:
        return GuardResult(ok=False, reason="dollar", offending=["$"])

    if "%" in text:
        return GuardResult(ok=False, reason="percent", offending=["%"])

    scanned = _mask_allowlist(text)
    for wm in _WORD_RE.finditer(scanned):
        raw = wm.group(0)
        pieces = raw.lower().replace("-", " ").split()
        bad = [p for p in pieces if p in NUMBER_WORDS]
        if bad:
            return GuardResult(ok=False, reason="number_word", offending=[raw])

    found = set(_PLACEHOLDER_RE.findall(text))
    unknown = found - set(allowed_ids)
    if unknown:
        return GuardResult(
            ok=False,
            reason="unknown_placeholder",
            offending=sorted(unknown),
        )
    missing = set(required_ids) - found
    if missing:
        return GuardResult(
            ok=False,
            reason="missing_required",
            offending=sorted(missing),
        )
    return GuardResult(ok=True)


def _dollars_after_bare(text: str, token: NumberToken) -> NumberToken | None:
    """Promote bare count to money when followed by dollar/dollars."""
    if token.kind != "count" or not isinstance(token.value, int):
        return None
    m = re.match(r"\s+dollars?\b", text[token.end :], re.IGNORECASE)
    if not m:
        return None
    return NumberToken(
        kind="money",
        value=token.value * 100,
        raw=text[token.start : token.end + m.end()],
        start=token.start,
        end=token.end + m.end(),
    )


def rendered_guard(
    text: str,
    public_facts: FactSet | dict[str, Fact],
    creditor_numbers: set[tuple[str, int | date]],
    private_blocklist: set[tuple[str, int | date]],
    *,
    ref: date | None = None,
) -> GuardResult:
    """Validate text after facts are filled in (ready to speak).

    Every extracted figure must match a PUBLIC fact or a creditor-said number.
    A hit on ``private_blocklist`` is ``boundary``, unless the same value is
    also a chosen PUBLIC fact (collision allowed). Commitment phrasing is
    checked last. ``ref`` anchors year-less dates during extraction.
    """
    if ref is None:
        ref = date.today()

    allowed = _fact_pairs(public_facts) | set(creditor_numbers)
    tokens = extract_tokens(text, ref=ref)

    # "2500 dollars" arrives as bare count; promote to money cents.
    promoted: list[NumberToken] = []
    skip_until = -1
    for tok in tokens:
        if tok.start < skip_until:
            continue
        money = _dollars_after_bare(text, tok)
        if money is not None:
            promoted.append(money)
            skip_until = money.end
        else:
            promoted.append(tok)

    for tok in promoted:
        if _cross_match(tok.kind, tok.value, private_blocklist):
            # Same cents as a PUBLIC fact for this action — not a leak.
            if _cross_match(tok.kind, tok.value, allowed):
                continue
            return GuardResult(ok=False, reason="boundary", offending=[tok.raw])

        if not _cross_match(tok.kind, tok.value, allowed):
            return GuardResult(ok=False, reason="unverified_number", offending=[tok.raw])

    if _COMMITMENT_RE.search(text):
        m = _COMMITMENT_RE.search(text)
        assert m is not None
        return GuardResult(ok=False, reason="commitment", offending=[m.group(0)])

    return GuardResult(ok=True)
