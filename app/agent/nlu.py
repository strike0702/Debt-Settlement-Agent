"""NLU: one JSON LLM call per rep turn, then deterministic post-verification.

``analyze`` asks the LLM for a ``TurnAnalysis``, retries once on validation
failure, then drops bad quotes / out-of-range values and sets ``verified`` from
``numbers.extract_tokens`` matches. ``NLU_MODE=oracle`` skips the LLM and uses
a caller-supplied ``TurnAnalysis`` (sim/tests). Does not update belief — that
is the orchestrator's job via ``BeliefState.observe``.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.numbers import extract_tokens
from app.config import Settings, get_settings
from app.domain.fields import FIELDS_BY_NAME
from app.llm.client import LLMUnavailable, strip_json_fences
from app.llm.prompts import nlu_messages
from app.store.audit import AuditLog

# Reasoning models (gpt-oss) spend completion budget on hidden reasoning.
_NLU_MAX_TOKENS = 800


class _LLM(Protocol):
    async def chat_text(
        self,
        role: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str: ...


class VerifiedTerm(BaseModel):
    """Extracted term after quote / number / prior-range checks."""

    field: str
    value: Any
    quote: str
    hedged: bool = False
    verified: bool


class VerifiedAnalysis(BaseModel):
    """Post-verified turn analysis for policy / belief updates."""

    terms: list[VerifiedTerm] = Field(default_factory=list)
    settlement_ask_pct: float | None = None
    ask_quote: str | None = None
    ask_verified: bool = False
    stance: str = "other"
    readback_response: str | None = None
    asks_client_private_info: bool = False
    demands_commitment: bool = False
    hostility: float = 0.0
    wants_to_end: bool = False
    asks_for_schedule: bool = False
    firm: bool = False
    # Orchestrator-only: post-proposal term change cue (not passed to policy).
    revises_terms: bool = False

    def to_turn_analysis(self) -> TurnAnalysis:
        """Drop verified flags for ``policy.decide``."""
        return TurnAnalysis(
            terms=[
                ExtractedTerm(
                    field=t.field,  # type: ignore[arg-type]
                    value=t.value,
                    quote=t.quote,
                    hedged=t.hedged,
                )
                for t in self.terms
            ],
            settlement_ask_pct=self.settlement_ask_pct,
            ask_quote=self.ask_quote,
            stance=self.stance,  # type: ignore[arg-type]
            readback_response=self.readback_response,  # type: ignore[arg-type]
            asks_client_private_info=self.asks_client_private_info,
            demands_commitment=self.demands_commitment,
            hostility=self.hostility,
            wants_to_end=self.wants_to_end,
            asks_for_schedule=self.asks_for_schedule,
            firm=self.firm,
        )


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

_PLAIN_YES_RE = re.compile(
    r"^\s*(?:yes|yep|yeah|correct|that's right|thats right|that is correct|"
    r"confirmed|confirm|right)\s*[.!]?\s*$",
    re.I,
)
_PLAIN_NO_RE = re.compile(
    r"^\s*(?:no|nope|incorrect|that's wrong|thats wrong|not correct|deny)\s*[.!]?\s*$",
    re.I,
)


def _verify_readback_response(claimed: str | None, utterance: str) -> str | None:
    """Keep confirm/deny only when the utterance contains a matching phrase."""
    if claimed == "confirm" and _READBACK_CONFIRM_RE.search(utterance):
        return "confirm"
    if claimed == "deny" and _READBACK_DENY_RE.search(utterance):
        return "deny"
    return None


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


def coerce_analysis_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize common LLM shape mistakes into ``TurnAnalysis`` fields."""
    out = dict(data)
    terms_raw = out.get("terms")
    terms: list[Any] = list(terms_raw) if isinstance(terms_raw, list) else []

    if not terms:
        for name in FIELDS_BY_NAME:
            blob = out.pop(name, None)
            if isinstance(blob, dict) and "value" in blob:
                terms.append(
                    {
                        "field": name,
                        "value": blob.get("value"),
                        "quote": blob.get("quote", ""),
                        "hedged": bool(blob.get("hedged", False)),
                    }
                )

    ask = out.get("settlement_ask_pct")
    if isinstance(ask, dict):
        out["settlement_ask_pct"] = ask.get("value")
        if not out.get("ask_quote"):
            out["ask_quote"] = ask.get("quote")

    out["terms"] = terms
    return out


def _parse_analysis(text: str) -> TurnAnalysis:
    raw = strip_json_fences(text)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("NLU JSON root must be an object")
    return TurnAnalysis.model_validate(coerce_analysis_payload(data))


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
    if not quote:
        return False
    bp = int(round(pct * 100))
    tokens = extract_tokens(quote, ref=ref)
    for t in tokens:
        if t.kind == "pct" and t.value == bp:
            return True
        if t.kind == "count" and t.value == int(pct):
            return True
    return False


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


# Deterministic stance repair when the LLM mislabels clear accept/reject lines.
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


def repair_stance(stance: str, utterance: str) -> str:
    """Override LLM stance when the utterance clearly accepts or rejects."""
    if _REJECT_STANCE_RE.search(utterance):
        return "reject"
    if _ACCEPT_STANCE_RE.search(utterance):
        return "accept"
    return stance


def repair_wants_to_end(wants_to_end: bool, utterance: str) -> bool:
    """Set wants_to_end when the rep clearly closes (thanks / bye / done)."""
    if wants_to_end:
        return True
    return _WANTS_TO_END_RE.search(utterance) is not None


def repair_asks_for_schedule(asks: bool, utterance: str) -> bool:
    """True when the rep asks for payment dates / schedule detail."""
    if asks:
        return True
    return _ASKS_FOR_SCHEDULE_RE.search(utterance) is not None


def repair_firm(firm: bool, utterance: str) -> bool:
    """True when the rep says the number is final / floor / cannot go lower."""
    if firm:
        return True
    return _FIRM_RE.search(utterance) is not None


def repair_revises_terms(utterance: str) -> bool:
    """True when the utterance cues a post-proposal term change."""
    return _REVISION_RE.search(utterance) is not None


def post_verify(
    analysis: TurnAnalysis,
    utterance: str,
    *,
    ref: date | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
) -> VerifiedAnalysis:
    """Deterministic quote / number / prior-range checks (PLAN §6.1)."""
    ref_d = ref or date.today()
    verified_terms: list[VerifiedTerm] = []

    def _log(event: str, payload: dict[str, Any]) -> None:
        if audit is not None and call_id is not None:
            audit.append(call_id, "nlu", event, payload)

    for term in analysis.terms:
        if not quote_in_utterance(term.quote, utterance):
            _log("nlu_rejected_quote", {"field": term.field, "quote": term.quote})
            continue
        if not _in_prior_range(term.field, term.value):
            _log(
                "nlu_rejected_range",
                {"field": term.field, "value": term.value, "quote": term.quote},
            )
            continue
        value: Any = term.value
        spec = FIELDS_BY_NAME.get(term.field)
        if spec is not None and spec.kind == "date" and isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                _log("nlu_rejected_date", {"field": term.field, "value": term.value})
                continue
        if (
            spec is not None
            and spec.kind == "date"
            and _quote_is_bare_year(term.quote)
        ):
            _log(
                "nlu_rejected_bare_year",
                {"field": term.field, "value": str(value), "quote": term.quote},
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
            _log(
                "nlu_rejected_quote",
                {"field": "settlement_ask_pct", "quote": ask_quote},
            )
            ask_pct = None
            ask_quote = None
        elif not _ask_matches(ask_pct, ask_quote, ref=ref_d):
            _log(
                "nlu_rejected_ask_value",
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

    stance = repair_stance(analysis.stance, utterance)
    readback = _verify_readback_response(analysis.readback_response, utterance)
    if analysis.readback_response and readback is None:
        _log(
            "nlu_rejected_readback",
            {"claimed": analysis.readback_response, "utterance": utterance[:120]},
        )

    return VerifiedAnalysis(
        terms=verified_terms,
        settlement_ask_pct=ask_pct,
        ask_quote=ask_quote,
        ask_verified=ask_verified,
        stance=stance,  # type: ignore[arg-type]
        readback_response=readback,
        asks_client_private_info=analysis.asks_client_private_info,
        demands_commitment=analysis.demands_commitment,
        hostility=analysis.hostility,
        wants_to_end=repair_wants_to_end(analysis.wants_to_end, utterance),
        asks_for_schedule=repair_asks_for_schedule(
            analysis.asks_for_schedule, utterance
        ),
        firm=repair_firm(analysis.firm, utterance),
        revises_terms=repair_revises_terms(utterance),
    )


async def analyze(
    utterance: str,
    last_agent_line: str,
    pending_readback: str | None,
    *,
    llm: _LLM | None = None,
    settings: Settings | None = None,
    oracle: TurnAnalysis | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
    ref: date | None = None,
) -> VerifiedAnalysis:
    """Run NLU (or oracle) then post-verify. Empty analysis on double validation fail."""
    cfg = settings or get_settings()
    empty = TurnAnalysis(stance="other")

    if cfg.nlu_mode == "oracle":
        if oracle is None:
            raise ValueError("nlu_mode=oracle requires oracle=TurnAnalysis")
        return post_verify(oracle, utterance, ref=ref, audit=audit, call_id=call_id)

    # Deterministic yes/no while a read-back is pending — skip the LLM.
    fast = try_fast_readback(utterance, pending_readback)
    if fast is not None:
        if audit is not None and call_id is not None:
            audit.append(
                call_id,
                "nlu",
                "fast_readback",
                {"response": fast.readback_response, "utterance": utterance[:80]},
            )
        return fast

    if llm is None:
        raise ValueError("llm is required when nlu_mode is not oracle")

    messages = nlu_messages(utterance, last_agent_line, pending_readback)
    analysis: TurnAnalysis | None = None
    last_err: str | None = None

    for attempt in range(2):
        try:
            msgs = list(messages)
            if attempt == 1 and last_err is not None:
                msgs = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            f"Previous JSON was invalid: {last_err}. "
                            "Reply with valid JSON only in the required shape."
                        ),
                    },
                ]
            text = await llm.chat_text("nlu", msgs, _NLU_MAX_TOKENS)
            analysis = _parse_analysis(text)
            break
        except LLMUnavailable:
            raise
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as e:
            last_err = str(e)
            analysis = None
            continue
        except Exception as e:
            last_err = str(e)
            analysis = None
            continue

    if analysis is None:
        if audit is not None and call_id is not None:
            audit.append(
                call_id,
                "nlu",
                "nlu_validation_failed",
                {"error": last_err},
            )
        analysis = empty

    verified = post_verify(analysis, utterance, ref=ref, audit=audit, call_id=call_id)

    # When a sim/oracle disposition is supplied under live NLU, keep verified
    # terms/ask from the LLM but overlay stance flags from ground truth so
    # phrasing artifacts cannot false-escalate.
    if oracle is not None:
        verified = verified.model_copy(
            update={
                "stance": repair_stance(oracle.stance, utterance),
                "readback_response": _verify_readback_response(
                    oracle.readback_response, utterance
                ),
                "asks_client_private_info": oracle.asks_client_private_info,
                "demands_commitment": oracle.demands_commitment,
                "hostility": oracle.hostility,
                "wants_to_end": repair_wants_to_end(oracle.wants_to_end, utterance),
                "asks_for_schedule": repair_asks_for_schedule(
                    oracle.asks_for_schedule, utterance
                ),
                "firm": repair_firm(oracle.firm, utterance),
            }
        )
    return verified
