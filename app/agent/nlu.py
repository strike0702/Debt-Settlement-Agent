"""NLU: one JSON LLM call per rep turn, then deterministic post-verification.

``analyze`` asks the LLM for a ``TurnAnalysis``, retries once on validation
failure, then drops bad quotes / out-of-range values and sets ``verified`` from
``numbers.extract_tokens`` matches. ``repair_*`` helpers then fix stance
(accept phrase, dominant short ack, never accept on injection) and OR each LLM
side-channel flag with a regex cue. ``repair_question`` keeps the Phase 24b
off-script question flag only with a question cue and never on a private-info
ask (that one is refused, not answered). ``NLU_MODE=oracle`` skips the LLM and uses
a caller-supplied ``TurnAnalysis`` (sim/tests). Rejected terms are audited and
also kept on ``VerifiedAnalysis.dropped`` for the decision trace. Does not
update belief — that is the orchestrator's job via ``BeliefState.observe``.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.numbers import extract_tokens
from app.agent.policy import ask_pct_to_bp
from app.config import Settings, get_settings
from app.domain.fields import FIELD_REGISTRY, FIELDS_BY_NAME
from app.domain.nlu_types import QUESTION_TOPICS
from app.llm.client import LLMUnavailable, strip_json_fences
from app.llm.prompts import nlu_messages
from app.store.audit import AuditLog

# Reasoning models (gpt-oss) spend completion budget on hidden reasoning. A
# default-effort NLU call was observed at 763 completion tokens against the old
# 800 cap, so one more reasoning step truncated the JSON (retry = 2x latency, or
# an empty analysis).
_NLU_MAX_TOKENS = 1200


class _LLM(Protocol):
    async def chat_text(
        self,
        role: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        *,
        json_mode: bool = False,
    ) -> str: ...


class VerifiedTerm(BaseModel):
    """Extracted term after quote / number / prior-range checks."""

    field: str
    value: Any
    quote: str
    hedged: bool = False
    verified: bool


class DroppedTerm(BaseModel):
    """A term (or the ask) ``post_verify`` rejected; ``reason`` = audit event minus ``nlu_``."""

    field: str
    value: Any = None
    reason: str
    quote: str | None = None


class VerifiedAnalysis(BaseModel):
    """Post-verified turn analysis for policy / belief updates."""

    terms: list[VerifiedTerm] = Field(default_factory=list)
    # Decision-trace only (never read by policy): what post_verify threw away.
    dropped: list[DroppedTerm] = Field(default_factory=list)
    settlement_ask_pct: float | None = None
    ask_quote: str | None = None
    ask_verified: bool = False
    # Dollar-total ask (Phase 39): verified against its quote here; converted to
    # ``settlement_ask_pct`` by ``resolve_amounts`` once the balance is known.
    settlement_ask_total_cents: int | None = None
    ask_total_quote: str | None = None
    # Set by ``resolve_amounts`` when the ask came from a dollar total.
    ask_total_bp: int | None = None
    # Verified dollar amount whose meaning (total vs per payment) is unclear.
    amount_ambiguous_cents: int | None = None
    amount_ambiguous_quote: str | None = None
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
    # Bare number for a cents field with no $/dollars/cents cue — ask which unit.
    cents_ambiguity_bare: int | None = None
    cents_ambiguity_field: str | None = None
    tiers_ambiguous: bool = False
    # Off-script question (Phase 24b); answered by ``app.agent.acts``, not policy.
    asks_question: bool = False
    question_topic: str | None = None

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
            tiers_ambiguous=self.tiers_ambiguous,
            asks_question=self.asks_question,
            question_topic=self.question_topic,  # type: ignore[arg-type]
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


def _field_for_ask_line(last_agent_line: str) -> str | None:
    """Return the registry field when the agent last asked that field's question."""
    line = (last_agent_line or "").strip()
    if not line:
        return None
    for spec in FIELD_REGISTRY:
        if line == spec.ask_text or spec.ask_text in line:
            return spec.name
    return None


_MONEY_UNIT_RE = re.compile(
    r"(?:\$|\bdollars?\b|\bcents?\b|\bbucks?\b)",
    re.IGNORECASE,
)
_BARE_DIGIT_QUOTE_RE = re.compile(r"^\d[\d,]*$")
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


def _utterance_has_money_unit(text: str) -> bool:
    return bool(_MONEY_UNIT_RE.search(text))


def _is_bare_digit_money(quote: str, utterance: str) -> bool:
    """True when the amount is only digits — no $ / dollars / cents / number-words."""
    if _utterance_has_money_unit(quote) or _utterance_has_money_unit(utterance):
        return False
    return bool(_BARE_DIGIT_QUOTE_RE.match(quote.strip()))


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


def _bare_count_from_quote(quote: str, *, ref: date) -> int | None:
    tokens = extract_tokens(quote, ref=ref)
    counts = [t for t in tokens if t.kind == "count"]
    money = [t for t in tokens if t.kind == "money"]
    if len(counts) == 1 and not money:
        return int(counts[0].value)
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

    total = out.get("settlement_ask_total_cents")
    if isinstance(total, dict):
        out["settlement_ask_total_cents"] = total.get("value")
        if not out.get("ask_total_quote"):
            out["ask_total_quote"] = total.get("quote")
    amb = out.get("amount_ambiguous")
    if isinstance(amb, dict) and out.get("amount_ambiguous_cents") is None:
        out["amount_ambiguous_cents"] = amb.get("cents", amb.get("value"))
        out["amount_ambiguous_quote"] = amb.get("quote")
    out.pop("amount_ambiguous", None)

    out["terms"] = terms
    # An off-list topic must not fail validation of the whole analysis.
    topic = out.get("question_topic")
    if topic is not None and topic not in QUESTION_TOPICS:
        out["question_topic"] = "other"
    return out


def coerce_tiers(raw: Any) -> list[tuple[int, int]] | None:
    """LLM / oracle tiers → engine ``[(from_payment, min_cents)]`` sorted; None if malformed.

    Items are ``{"from_payment", "min_cents"}`` dicts (LLM) or 2-int pairs
    (oracle). Any bad item, a non-list, or a repeated ``from_payment``
    rejects the whole value rather than guessing.
    """
    if not isinstance(raw, list):
        return None
    out: list[tuple[int, int]] = []
    for item in raw:
        if isinstance(item, dict):
            if set(item) != {"from_payment", "min_cents"}:
                return None
            pair = (item["from_payment"], item["min_cents"])
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            pair = (item[0], item[1])
        else:
            return None
        frm, cents = pair
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in pair):
            return None
        if frm < 1 or cents <= 0:
            return None
        out.append((frm, cents))
    if len({frm for frm, _ in out}) != len(out):
        return None
    return sorted(out)


# "for the first three payments" reads as an up-to cap, not the engine's from-N
# shape; ask the rep to restate instead of converting.
_TIERS_FIRST_N_RE = re.compile(r"\b(?:first|initial)\s+(?:\w+\s+)?payments\b", re.IGNORECASE)


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
    reading decides a guarded line (Phase 45, ledger 31.1 / 31.2).
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
    ("not the lowest we can go") does not set the flag (Phase 45).
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


def repair_hostility(hostility: float, utterance: str) -> float:
    """Keep LLM hostility only when the utterance has hostile cues; else 0."""
    h = max(0.0, min(1.0, float(hostility)))
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
    """Keep a dollar amount (Phase 39 slots) only when its quote verifies; else drop + audit."""
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


# --- Phase 39: dollar-total asks and total-vs-per-payment ambiguity ---------
#
# ``post_verify`` cannot convert a dollar total to bp (it has no balance), so
# the orchestrator calls ``resolve_amounts`` after NLU. It either turns a
# verified total into the ordinary ``settlement_ask_pct`` or returns a pending
# clarify; it never changes an amount on its own.

# A % ask and a dollar total agree when they differ by at most half a percent
# point (the rep rounding the percent to a whole number).
_PCT_TOTAL_TOLERANCE_BP = 50

# Total cues: the amount is the whole settlement. "pay $X by <date>" is a
# deadline for a sum, not a per-payment floor.
_TOTAL_CUE_RE = re.compile(
    r"\b(?:total|in full|to settle|settle (?:it |this |the account )?for|lump[- ]sum)\b"
    r"|\bpay\s+\$\s?[\d,]+(?:\.\d{2})?\s+by\b",
    re.IGNORECASE,
)
# Per-payment cues: the amount is a floor for each payment.
_PER_PAYMENT_CUE_RE = re.compile(
    r"\b(?:per (?:payment|month|installment)|each|every|a month|monthly|minimum"
    r"|at least|a payment)\b",
    re.IGNORECASE,
)
# Answers to "Is $X the total settlement, or the minimum for each payment?".
_ANSWER_TOTAL_RE = re.compile(
    r"\b(?:total|in full|settle|settlement|whole|altogether|all in|the first|first one|lump)\b",
    re.IGNORECASE,
)
_ANSWER_PER_RE = re.compile(
    r"\b(?:per|each|every|minimum|monthly|a month|installments?|the second|second one)\b",
    re.IGNORECASE,
)


# A payment count with these words is a maximum, not the count.
_COUNT_CAP_RE = re.compile(
    r"\b(?:up to|at most|max(?:imum)?|no more than|or fewer|or less|as many as)\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"[,.;!?]|\bbut\b", re.IGNORECASE)

# Phase 46b, code-side question for a verified TOTAL (whatever the model said):
# in the same sentence as an exact payment count ("3 even payments"), or as
# "pay $X by <date>", the amount may as well be per payment, so ask. An
# explicit total cue in that sentence ("in total", "altogether") settles it.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_EXPLICIT_TOTAL_RE = re.compile(
    r"\b(?:total|altogether|all together|all in|in full|lump[- ]sum|in one payment"
    r"|settle (?:it |this |the account )?for|settlement of)\b",
    re.IGNORECASE,
)
_NUMBER_WORD = (
    r"(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"(?:thir|four|fif|six|seven|eigh|nine)teen|twenty(?:[- ](?:one|two|three|four))?)"
)
_EXACT_COUNT_RE = re.compile(
    rf"\b(?:\d+|{_NUMBER_WORD})\s+(?:(?:even|equal|monthly|separate|fixed)\s+)?"
    r"(?:payments|installments|instalments)\b",
    re.IGNORECASE,
)
# A sentence that states the debt ("original balance of $6,000", "the client
# owes $6,000") is not an ask in any shape: the shape question never fires on it.
_BALANCE_STATEMENT_RE = re.compile(
    r"\b(?:balance|owes?|owed|owing|outstanding|original amount|debt of|account is at)\b",
    re.IGNORECASE,
)
_PAY_BEFORE_RE = re.compile(r"\bpay\s+$", re.IGNORECASE)
_BY_AFTER_RE = re.compile(r"^\s+by\b", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\b(?:not|isn't|isnt|no)\b", re.IGNORECASE)


def total_cents_to_bp(total_cents: int, balance_cents: int) -> tuple[int, Decimal]:
    """``(bp, exact)``: a dollar total as bp of the balance, rounded UP to a whole bp.

    Rounding up never favours the client: we never believe the ask is lower
    than the rep said.
    """
    exact = Decimal(total_cents) * Decimal(10000) / Decimal(balance_cents)
    return int(exact.to_integral_value(rounding=ROUND_CEILING)), exact


def bp_to_ask_pct(bp: int) -> float:
    """Inverse of ``ask_pct_to_bp`` for a whole bp (4567 → 45.67, round-trips exactly)."""
    return float(Decimal(bp) / Decimal(100))


def _amount_pending(
    cents: int, quote: str | None, trigger: str, analysis: VerifiedAnalysis
) -> dict[str, Any]:
    """Pending clarify state. A verified % ask the clarify holds back rides along."""
    pending: dict[str, Any] = {"cents": cents, "quote": quote, "trigger": trigger}
    if analysis.settlement_ask_pct is not None and analysis.ask_verified:
        pending["pct"] = analysis.settlement_ask_pct
        pending["pct_quote"] = analysis.ask_quote
    return pending


def total_shape_trigger(total_cents: int, utterance: str) -> str | None:
    """Question trigger for a verified dollar total, or None when it reads as a total.

    Looks only at the sentence(s) that say the amount. ``total_pay_by``: "pay
    $X by <date>"; ``total_with_count``: an exact payment count in the same
    sentence (a capped "up to 6 payments" is not exact). An explicit total cue
    or a balance statement ("original balance of $6,000") in that sentence
    means nothing is asked.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(utterance):
        hits = [
            t
            for t in extract_tokens(sentence)
            if t.kind == "money" and t.value == total_cents
        ]
        if not hits or _EXPLICIT_TOTAL_RE.search(sentence):
            continue
        if _BALANCE_STATEMENT_RE.search(sentence):
            continue
        for tok in hits:
            if _PAY_BEFORE_RE.search(sentence[: tok.start]) and _BY_AFTER_RE.search(
                sentence[tok.end :]
            ):
                return "total_pay_by"
        if _EXACT_COUNT_RE.search(sentence) and not _COUNT_CAP_RE.search(sentence):
            return "total_with_count"
    return None


def resolve_amounts(
    verified: VerifiedAnalysis,
    utterance: str,
    *,
    balance_cents: int,
    check_min_payment: bool = True,
    audit: AuditLog | None = None,
    call_id: str | None = None,
) -> tuple[VerifiedAnalysis, dict[str, Any] | None]:
    """Turn a verified dollar total into the ask, or return a pending clarify.

    Returns ``(analysis, pending)``. With ``pending`` set, the caller asks
    "total or per payment?" and ``analysis`` has the doubtful amount (and any
    disagreeing ask) removed so the rest of the turn can still be applied.
    Triggers, first match: NLU ``amount_ambiguous``; % and total disagree;
    total above the balance; a total with no agreeing % in a per-payment shape
    (``total_shape_trigger``: "pay $X by", or an exact count in the same
    sentence); a ``min_payment_cents`` term that is implausible
    (amount × an exact payment count said this turn, else × 1, > balance),
    sits under a total cue with no per-payment cue, or equals a total the NLU
    also gave.
    ``check_min_payment=False`` after the rep already said "per payment".
    """

    def _log(event: str, payload: dict[str, Any]) -> None:
        if audit is not None and call_id is not None:
            audit.append(call_id, "nlu", event, payload)

    def _clarify(
        cents: int, quote: str | None, trigger: str, *, drop_ask: bool = False
    ) -> tuple[VerifiedAnalysis, dict[str, Any]]:
        pending = _amount_pending(cents, quote, trigger, verified)
        update: dict[str, Any] = {
            "terms": [
                t
                for t in verified.terms
                if not (t.field == "min_payment_cents" and t.value == cents)
            ],
            "settlement_ask_total_cents": None,
            "ask_total_quote": None,
            "amount_ambiguous_cents": None,
            "amount_ambiguous_quote": None,
        }
        if drop_ask:
            update.update(settlement_ask_pct=None, ask_quote=None, ask_verified=False)
        _log("nlu_amount_ambiguous", {"cents": cents, "quote": quote, "trigger": trigger})
        return verified.model_copy(update=update), pending

    if verified.amount_ambiguous_cents is not None:
        return _clarify(
            verified.amount_ambiguous_cents, verified.amount_ambiguous_quote, "nlu_flag"
        )

    total = verified.settlement_ask_total_cents
    if total is not None and balance_cents > 0:
        bp, exact = total_cents_to_bp(total, balance_cents)
        pct_bp = (
            ask_pct_to_bp(verified.settlement_ask_pct)
            if verified.settlement_ask_pct is not None and verified.ask_verified
            else None
        )
        if pct_bp is not None and abs(exact - Decimal(pct_bp)) > _PCT_TOTAL_TOLERANCE_BP:
            return _clarify(total, verified.ask_total_quote, "pct_total_disagree", drop_ask=True)
        if bp > 10000:
            return _clarify(total, verified.ask_total_quote, "total_exceeds_balance")
        shape = total_shape_trigger(total, utterance) if pct_bp is None else None
        if shape is not None:
            return _clarify(total, verified.ask_total_quote, shape)
        chosen = bp if pct_bp is None else max(bp, pct_bp)
        _log(
            "nlu_ask_total_to_bp",
            {
                "total_cents": total,
                "balance_cents": balance_cents,
                "exact_bp": str(exact),
                "rounded_bp": bp,
                "pct_bp": pct_bp,
                "ask_bp": chosen,
            },
        )
        verified = verified.model_copy(
            update={
                "settlement_ask_pct": bp_to_ask_pct(chosen),
                "ask_quote": verified.ask_quote or verified.ask_total_quote,
                "ask_verified": True,
                "ask_total_bp": chosen,
            }
        )

    if check_min_payment:
        # "up to 12 payments" is a cap (one payment is still allowed), so only an
        # exact count said in the same turn ("only 3 even payments") multiplies.
        counts = [
            int(t.value)
            for t in verified.terms
            if t.field == "max_payments" and isinstance(t.value, int)
        ]
        exact_count = bool(counts) and not _COUNT_CAP_RE.search(utterance)
        count = min(counts) if exact_count else 1
        per_cue = bool(_PER_PAYMENT_CUE_RE.search(utterance))
        for t in verified.terms:
            if t.field != "min_payment_cents" or not isinstance(t.value, int):
                continue
            if balance_cents > 0 and t.value * count > balance_cents:
                return _clarify(t.value, t.quote, "min_exceeds_balance")
            if not per_cue and _TOTAL_CUE_RE.search(utterance):
                return _clarify(t.value, t.quote, "total_cue")
            if total is not None and t.value == total:
                return _clarify(t.value, t.quote, "both_readings")
    return verified, None


def try_resolve_amount_clarify(
    utterance: str,
    pending: dict[str, Any],
    *,
    ref: date | None = None,
) -> VerifiedAnalysis | None:
    """Resolve "total or per payment?" deterministically; ``None`` when still unclear.

    "the total" → a verified dollar-total ask (``resolve_amounts`` converts it);
    "per payment" → a verified ``min_payment_cents`` term. Either way a % ask
    the clarify held back comes back, except after a %-vs-total disagreement
    answered "the total". A restated amount counts only with one of those cues
    ("$450 total"); a bare amount, "yes", or both cues stay unclear.
    """
    text = utterance.strip()
    # "the total, not per payment": a negated clause names the reading the rep
    # rules out, so only un-negated clauses count. Unpunctuated negation stays
    # unclear (asked again) rather than guessed.
    clauses = [
        c for c in _CLAUSE_SPLIT_RE.split(text) if c.strip() and not _NEGATION_RE.search(c)
    ]
    is_total = any(_ANSWER_TOTAL_RE.search(c) for c in clauses)
    is_per = any(_ANSWER_PER_RE.search(c) for c in clauses)
    if is_total == is_per:
        return None
    ref_d = ref or date.today()
    cents = int(pending["cents"])
    quote = str(pending.get("quote") or "")
    money = [t for t in extract_tokens(text, ref=ref_d) if t.kind == "money"]
    if len(money) > 1:
        return None
    if money:
        cents, quote = int(money[0].value), money[0].raw
    if is_total:
        out = VerifiedAnalysis(
            stance="info",
            settlement_ask_total_cents=cents,
            ask_total_quote=quote or None,
        )
        # A % that disagreed with this total is what the rep just overruled.
        if pending.get("pct") is not None and pending.get("trigger") != "pct_total_disagree":
            out = out.model_copy(
                update={
                    "settlement_ask_pct": float(pending["pct"]),
                    "ask_quote": pending.get("pct_quote"),
                    "ask_verified": True,
                }
            )
        return out
    if not _in_prior_range("min_payment_cents", cents):
        return None
    out = VerifiedAnalysis(
        terms=[
            VerifiedTerm(
                field="min_payment_cents",
                value=cents,
                quote=quote,
                hedged=False,
                verified=True,
            )
        ],
        stance="info",
    )
    if pending.get("pct") is not None:
        out = out.model_copy(
            update={
                "settlement_ask_pct": float(pending["pct"]),
                "ask_quote": pending.get("pct_quote"),
                "ask_verified": True,
            }
        )
    return out


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

    # Bare number/enum reply to the agent's last ASK — skip the LLM.
    fast_ask = try_fast_field_answer(utterance, last_agent_line, ref=ref)
    if fast_ask is not None:
        if audit is not None and call_id is not None:
            audit.append(
                call_id,
                "nlu",
                "fast_field_answer",
                {
                    "field": fast_ask.terms[0].field if fast_ask.terms else None,
                    "value": fast_ask.terms[0].value if fast_ask.terms else None,
                    "utterance": utterance[:80],
                },
            )
        return fast_ask

    if llm is None:
        raise ValueError("llm is required when nlu_mode is not oracle")

    messages = nlu_messages(utterance, last_agent_line, pending_readback, ref=ref)
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
            text = await llm.chat_text("nlu", msgs, _NLU_MAX_TOKENS, json_mode=True)
            analysis = _parse_analysis(text)
            break
        except LLMUnavailable:
            raise
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as e:
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
        has_terms = bool(verified.terms) or verified.settlement_ask_pct is not None
        verified = verified.model_copy(
            update=_repair_dispositions(oracle, utterance, has_terms=has_terms)
        )
    return verified
