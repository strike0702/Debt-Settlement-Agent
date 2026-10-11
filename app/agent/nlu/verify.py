"""Post-verification: which of the model's extracted terms the agent may trust.

``post_verify`` checks every term against the rep's own words: its quote must
appear in the line, a number parsed from the quote (``app.agent.numbers``)
must match the value for the field's kind, and the value must sit in the
field's prior range; bare digits for a money field, a bare year as a date,
tiers phrased as "the first N payments" and a payment-structure word that is
not about the payments are rejected or flagged. Dropped terms are audited and
kept on ``VerifiedAnalysis.dropped``. Then it applies the ``cues`` repairs.
The shared quote helpers here are also used by ``fast_paths`` and ``amounts``.
Dollar totals are only quote-checked here; ``amounts`` converts them.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.agent.nlu.cues import _repair_dispositions, repair_question, repair_revises_terms
from app.agent.nlu.models import DroppedTerm, VerifiedAnalysis, VerifiedTerm
from app.agent.nlu.parse import coerce_tiers
from app.agent.nlu.quotes import (
    _in_prior_range,
    _is_bare_digit_money,
    _utterance_has_money_unit,
    normalize_for_quote,
    quote_in_utterance,
)
from app.agent.nlu_types import TurnAnalysis
from app.agent.numbers import extract_tokens
from app.agent.policy import ask_pct_to_bp
from app.domain.fields import FIELDS_BY_NAME
from app.store.audit import AuditLog

# Bare year like "2026" with no month/day — not a payment date.
_BARE_YEAR_QUOTE_RE = re.compile(r"^\s*(?:year\s+)?((?:19|20)\d{2})\s*$", re.I)
_MONTH_OR_DAY_RE = re.compile(
    r"(?:"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b"
    r"|\b(?:\d{1,2})(?:st|nd|rd|th)?\b"
    r"|[/.-]"
    r")",
    re.I,
)


def _quote_is_bare_year(quote: str) -> bool:
    """True when the quote is only a year with no month/day cue."""
    q = quote.strip()
    if not q:
        return False
    if _BARE_YEAR_QUOTE_RE.match(q) and not _MONTH_OR_DAY_RE.search(q):
        return True
    # ISO year-only or year with no month/day digits beyond the year.
    if re.fullmatch(r"(?:19|20)\d{2}", q):
        return True
    return False


def _bare_count_from_quote(quote: str, *, ref: date) -> int | None:
    tokens = extract_tokens(quote, ref=ref)
    counts = [t for t in tokens if t.kind == "count"]
    money = [t for t in tokens if t.kind == "money"]
    if len(counts) == 1 and not money:
        return int(counts[0].value)
    return None


# "for the first three payments" reads as an up-to cap, not the engine's from-N
# shape; ask the rep to restate instead of converting.
_TIERS_FIRST_N_RE = re.compile(r"\b(?:first|initial)\s+(?:\w+\s+)?payments\b", re.IGNORECASE)


def _value_matches_tokens(field: str, value: Any, quote: str, *, ref: date) -> bool:
    """True when a number parsed from ``quote`` matches ``value`` for the field kind."""
    spec = FIELDS_BY_NAME.get(field)
    if spec is None:
        return False
    tokens = extract_tokens(quote, ref=ref)
    if spec.kind == "cents":
        if not isinstance(value, int):
            return False
        for t in tokens:
            if t.kind == "money" and t.value == value:
                return True
            if t.kind == "count" and t.value * 100 == value:
                return True
        return False
    if spec.kind == "int":
        if not isinstance(value, int):
            return False
        return any(t.kind in ("count", "ordinal") and t.value == value for t in tokens)
    if spec.kind == "enum":
        # Word-boundary only — "flexible" must not match inside "inflexible".
        return quote_in_utterance(str(value), quote)
    if spec.kind == "date":
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                return False
        return any(t.kind == "date" and t.value == value for t in tokens)
    if spec.kind == "tiers":
        # Tier structures are not number-verified; force TENTATIVE / read-back.
        return False
    return False


def _ask_matches(pct: float, quote: str | None, *, ref: date) -> bool:
    """True when quote tokens match ``ask_pct_to_bp(pct)`` (HALF_UP), not float round."""
    if not quote:
        return False
    bp = ask_pct_to_bp(pct)
    tokens = extract_tokens(quote, ref=ref)
    for t in tokens:
        if t.kind == "pct" and t.value == bp:
            return True
        if t.kind == "count" and t.value == int(pct):
            return True
    return False


# A percent that names something other than the settlement share: "the 100%
# balance", "0% interest", "100% negotiable", "the 50% installment". Live NLU
# read these as the ask (A/B s0007_006), and the policy then accepted aloud.
# "N% of the balance" and "settle at N%" are real asks and stay.
_PCT_SIGN_RE = re.compile(r"%|\bpercent\b")
_NON_ASK_AFTER_PCT_RE = re.compile(
    r"\s+(?:balance|interest|installments?|payments?|down|negotiable|clear|mandatory"
    r"|deviation|flexibility|balloon|understanding|rate)\b"
)


def _pct_names_non_ask(quote: str, utterance: str) -> bool:
    """True when the quoted percent is directly followed by a non-ask noun."""
    low, q = utterance.lower(), quote.lower().strip()
    idx = low.find(q)
    span = low[idx:] if idx >= 0 else q
    m = _PCT_SIGN_RE.search(span)
    if m is None or (idx >= 0 and m.start() > len(q)):
        return False
    return _NON_ASK_AFTER_PCT_RE.match(span, m.end()) is not None


def _value_matches_utterance_span(
    field: str, value: Any, quote: str, utterance: str, *, ref: date
) -> bool:
    """True when an utterance token spanning the quote matches ``value``.

    Prefer maximal ``extract_tokens(utterance)`` spans over re-parsing a possibly
    truncated quote string in isolation (blocks ``250`` vs ``1250`` style tricks
    even if a boundary check were bypassed).
    """
    q = normalize_for_quote(quote)
    if not q:
        return False
    u_norm = normalize_for_quote(utterance)
    # Find quote as whole words in normalized utterance, then check raw tokens.
    if not re.search(rf"(?<!\w){re.escape(q)}(?!\w)", u_norm):
        return False
    # Prefer tokens whose raw text normalizes to the quote (exact span).
    for tok in extract_tokens(utterance, ref=ref):
        if normalize_for_quote(tok.raw) != q:
            continue
        if _value_matches_tokens(field, value, tok.raw, ref=ref):
            return True
    # Fallback: quote itself parses to the claimed value (multi-word quotes).
    return _value_matches_tokens(field, value, quote, ref=ref)


def _amount_matches(cents: int, quote: str, utterance: str, *, ref: date) -> bool:
    """True when ``quote`` names ``cents`` as money ($ / dollars / a money token).

    A bare "420" with no unit is not accepted: it could be dollars or cents.
    """
    if not quote_in_utterance(quote, utterance):
        return False
    if not _value_matches_utterance_span("min_payment_cents", cents, quote, utterance, ref=ref):
        return False
    if _utterance_has_money_unit(quote):
        return True
    q = normalize_for_quote(quote)
    return any(
        t.kind == "money" and t.value == cents and normalize_for_quote(t.raw) == q
        for t in extract_tokens(utterance, ref=ref)
    )


def _verify_amount(
    field: str,
    cents: Any,
    quote: str | None,
    utterance: str,
    *,
    ref: date,
    drop: Any,
) -> tuple[int | None, str | None]:
    """Keep a dollar-amount slot only when its quote verifies; else drop + audit."""
    if cents is None:
        return None, None
    if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
        drop("nlu_rejected_amount_value", field, cents, quote, {"field": field, "value": cents})
        return None, None
    if not quote or not quote_in_utterance(quote, utterance):
        drop("nlu_rejected_quote", field, cents, quote, {"field": field, "quote": quote})
        return None, None
    if not _amount_matches(cents, quote, utterance, ref=ref):
        drop(
            "nlu_rejected_amount_value",
            field,
            cents,
            quote,
            {"field": field, "value": cents, "quote": quote},
        )
        return None, None
    return cents, quote


# A payment-structure word counts only when it is about the payments.
# "We can be flexible on other terms" (heard on a real test call) is not a flexible
# schedule, and "can't even do that" is not even payments. The word must sit near
# a schedule noun ("flexible payments", "an even schedule") or a variation
# phrase ("the payments can vary"), or answer our structure question ("flexible
# works") — and never be "flexible on / about / with" something else. "Balloon"
# has no other meaning on a payment call, so it needs only the last condition.
_STRUCTURE_NOUN_RE = re.compile(
    r"\b(?:payments?|schedules?|structures?|plans?|installments?|instalments?|amounts?"
    r"|evenly|equal|vary|varies|varying|lump|final payment|end)\b"
)
_STRUCTURE_OFF_TOPIC_RE = re.compile(
    r"\s+(?:on|about|with|regarding|around|in|when|if)\b(?!\s+(?:the\s+|a\s+)?"
    r"(?:payments?|schedules?|structure|plan|installments?|amounts?)\b)"
)
_STRUCTURE_QUESTION_RE = re.compile(
    r"\b(?:payment structure|balloon|even payments|flexible schedule)\b", re.IGNORECASE
)
# Words on either side of the structure word that may hold its noun.
_STRUCTURE_WINDOW_WORDS = 4
_STRUCTURE_CLAUSE_RE = re.compile(r"[.!?;]|\b(?:but|though|although)\b")


def structure_word_ok(value: str, utterance: str, *, last_agent_line: str = "") -> bool:
    """True when ``value`` (even / balloon / flexible) in ``utterance`` names a payment structure.

    Looks at every clause that says the word: one with a schedule noun within
    four words, any clause when the agent's last line asked about the
    structure, or any "balloon" passes — unless the word is followed by
    "on / about / with …" something other than the payments.
    """
    low = utterance.lower()
    asked = bool(_STRUCTURE_QUESTION_RE.search(last_agent_line or ""))
    word_re = re.compile(rf"\b{re.escape(value.lower())}\b")
    for clause in _STRUCTURE_CLAUSE_RE.split(low):
        for m in word_re.finditer(clause):
            if _STRUCTURE_OFF_TOPIC_RE.match(clause, m.end()):
                continue
            before = clause[: m.start()].split()[-_STRUCTURE_WINDOW_WORDS:]
            after = clause[m.end() :].split()[:_STRUCTURE_WINDOW_WORDS]
            if asked or value == "balloon":
                # "Balloon" has no other meaning on a payment call; "flexible"
                # and "even" do, so they need their schedule noun.
                return True
            if _STRUCTURE_NOUN_RE.search(" ".join(before + after)):
                return True
    return False


def post_verify(
    analysis: TurnAnalysis,
    utterance: str,
    *,
    ref: date | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
    last_agent_line: str = "",
) -> VerifiedAnalysis:
    """Deterministic quote / number / prior-range checks.

    ``last_agent_line`` lets a bare structure word answer our structure question
    (``structure_word_ok``); without it the word needs a schedule noun nearby.
    """
    ref_d = ref or date.today()
    verified_terms: list[VerifiedTerm] = []
    cents_ambiguity_bare: int | None = None
    cents_ambiguity_field: str | None = None
    tiers_ambiguous = False

    dropped: list[DroppedTerm] = []

    def _log(event: str, payload: dict[str, Any]) -> None:
        if audit is not None and call_id is not None:
            audit.append(call_id, "nlu", event, payload)

    def _drop(
        event: str, field: str, value: Any, quote: str | None, payload: dict[str, Any]
    ) -> None:
        """Audit a rejected term and keep it for the turn trace."""
        _log(event, payload)
        dropped.append(
            DroppedTerm(field=field, value=value, reason=event.removeprefix("nlu_"), quote=quote)
        )

    for term in analysis.terms:
        if not quote_in_utterance(term.quote, utterance):
            _drop(
                "nlu_rejected_quote",
                term.field,
                term.value,
                term.quote,
                {"field": term.field, "quote": term.quote},
            )
            continue
        value: Any = term.value
        spec = FIELDS_BY_NAME.get(term.field)
        if spec is not None and spec.kind == "tiers":
            tiers = coerce_tiers(value)
            if tiers is None:
                _drop(
                    "nlu_rejected_tiers",
                    term.field,
                    value,
                    term.quote,
                    {"value": value, "quote": term.quote},
                )
                continue
            if tiers and _TIERS_FIRST_N_RE.search(utterance):
                tiers_ambiguous = True
                _drop(
                    "nlu_tiers_ambiguous",
                    term.field,
                    [list(t) for t in tiers],
                    term.quote,
                    {"value": tiers, "quote": term.quote},
                )
                continue
            value = tiers
        # Pure digit amount for a cents field → clarify dollars vs cents.
        if (
            spec is not None
            and spec.kind == "cents"
            and cents_ambiguity_bare is None
            and _is_bare_digit_money(term.quote, utterance)
        ):
            bare = _bare_count_from_quote(term.quote, ref=ref_d)
            if bare is None and isinstance(value, int):
                bare = value // 100 if value >= 1000 and value % 100 == 0 else value
            if bare is not None and bare > 0:
                dollars = bare * 100
                if _in_prior_range(term.field, dollars) or _in_prior_range(
                    term.field, bare
                ):
                    cents_ambiguity_bare = bare
                    cents_ambiguity_field = term.field
                    _drop(
                        "nlu_cents_ambiguity",
                        term.field,
                        bare,
                        term.quote,
                        {"field": term.field, "bare": bare, "quote": term.quote},
                    )
                    continue
        if not _in_prior_range(term.field, value):
            # Forgotten *100 with an explicit dollar cue → promote.
            if (
                spec is not None
                and spec.kind == "cents"
                and isinstance(value, int)
                and _utterance_has_money_unit(utterance)
                and _in_prior_range(term.field, value * 100)
            ):
                value = value * 100
                _log(
                    "nlu_repaired_cents",
                    {
                        "field": term.field,
                        "from": term.value,
                        "to": value,
                        "quote": term.quote,
                    },
                )
            else:
                _drop(
                    "nlu_rejected_range",
                    term.field,
                    value,
                    term.quote,
                    {"field": term.field, "value": value, "quote": term.quote},
                )
                continue
        if spec is not None and spec.kind == "date" and isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                _drop(
                    "nlu_rejected_date",
                    term.field,
                    term.value,
                    term.quote,
                    {"field": term.field, "value": term.value},
                )
                continue
        if (
            spec is not None
            and spec.kind == "date"
            and _quote_is_bare_year(term.quote)
        ):
            _drop(
                "nlu_rejected_bare_year",
                term.field,
                str(value),
                term.quote,
                {"field": term.field, "value": str(value), "quote": term.quote},
            )
            continue
        if (
            term.field == "payment_structure"
            and isinstance(value, str)
            # A paraphrase ("equal installments") stays unverified and is read back.
            and quote_in_utterance(value, utterance)
            and not structure_word_ok(value, utterance, last_agent_line=last_agent_line)
        ):
            _drop(
                "nlu_rejected_structure",
                term.field,
                value,
                term.quote,
                {"field": term.field, "value": value, "quote": term.quote},
            )
            continue
        if spec is not None and spec.kind == "enum":
            # Word-boundary only — "even" must not match quote "evening".
            matched = quote_in_utterance(str(value), term.quote)
        elif spec is not None and spec.kind == "tiers":
            matched = False
        else:
            matched = _value_matches_utterance_span(
                term.field, value, term.quote, utterance, ref=ref_d
            )
        verified_terms.append(
            VerifiedTerm(
                field=term.field,
                value=value,
                quote=term.quote,
                hedged=term.hedged,
                verified=matched,
            )
        )

    ask_pct = analysis.settlement_ask_pct
    ask_quote = analysis.ask_quote
    ask_verified = False
    if ask_pct is not None:
        # Missing quote ≡ rejected; only keep ask when quote matches AND value matches.
        if not ask_quote or not quote_in_utterance(ask_quote, utterance):
            _drop(
                "nlu_rejected_quote",
                "settlement_ask_pct",
                ask_pct,
                ask_quote,
                {"field": "settlement_ask_pct", "quote": ask_quote},
            )
            ask_pct = None
            ask_quote = None
        elif not _ask_matches(ask_pct, ask_quote, ref=ref_d) or _pct_names_non_ask(
            ask_quote, utterance
        ):
            _drop(
                "nlu_rejected_ask_value",
                "settlement_ask_pct",
                ask_pct,
                ask_quote,
                {
                    "field": "settlement_ask_pct",
                    "pct": ask_pct,
                    "quote": ask_quote,
                },
            )
            ask_pct = None
            ask_quote = None
        else:
            ask_verified = True

    # Dollar-total ask and ambiguous amount: same rule as the % ask — the quote
    # must be in the utterance and its money token must equal the value.
    total_cents, total_quote = _verify_amount(
        "settlement_ask_total_cents",
        analysis.settlement_ask_total_cents,
        analysis.ask_total_quote,
        utterance,
        ref=ref_d,
        drop=_drop,
    )
    amb_cents, amb_quote = _verify_amount(
        "amount_ambiguous_cents",
        analysis.amount_ambiguous_cents,
        analysis.amount_ambiguous_quote,
        utterance,
        ref=ref_d,
        drop=_drop,
    )

    has_terms = (
        bool(verified_terms)
        or ask_pct is not None
        or cents_ambiguity_bare is not None
        or tiers_ambiguous
        or total_cents is not None
        or amb_cents is not None
    )
    dispositions = _repair_dispositions(analysis, utterance, has_terms=has_terms)
    asks_question, question_topic = repair_question(
        analysis.asks_question,
        analysis.question_topic,
        utterance,
        asks_private=dispositions["asks_client_private_info"],
    )
    if analysis.readback_response and dispositions["readback_response"] is None:
        _log(
            "nlu_rejected_readback",
            {"claimed": analysis.readback_response, "utterance": utterance[:120]},
        )

    return VerifiedAnalysis(
        terms=verified_terms,
        dropped=dropped,
        settlement_ask_pct=ask_pct,
        ask_quote=ask_quote,
        ask_verified=ask_verified,
        settlement_ask_total_cents=total_cents,
        ask_total_quote=total_quote,
        amount_ambiguous_cents=amb_cents,
        amount_ambiguous_quote=amb_quote,
        **dispositions,
        revises_terms=repair_revises_terms(utterance),
        cents_ambiguity_bare=cents_ambiguity_bare,
        cents_ambiguity_field=cents_ambiguity_field,
        tiers_ambiguous=tiers_ambiguous,
        asks_question=asks_question,
        question_topic=question_topic,
    )
