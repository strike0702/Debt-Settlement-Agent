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
from app.llm.client import strip_json_fences
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
        )


def normalize_for_quote(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (quote substring check)."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def quote_in_utterance(quote: str, utterance: str) -> bool:
    """True when normalized quote is a substring of normalized utterance."""
    q = normalize_for_quote(quote)
    if not q:
        return False
    return q in normalize_for_quote(utterance)


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
        return normalize_for_quote(str(value)) in normalize_for_quote(quote)
    if spec.kind == "date":
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                return False
        return any(t.kind == "date" and t.value == value for t in tokens)
    if spec.kind == "tiers":
        return True
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
        matched = _value_matches_tokens(term.field, value, term.quote, ref=ref_d)
        if spec is not None and spec.kind == "enum":
            matched = normalize_for_quote(str(value)) in normalize_for_quote(term.quote)
        if spec is not None and spec.kind == "tiers":
            matched = True
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
        if ask_quote and not quote_in_utterance(ask_quote, utterance):
            _log(
                "nlu_rejected_quote",
                {"field": "settlement_ask_pct", "quote": ask_quote},
            )
            ask_pct = None
            ask_quote = None
        elif ask_quote:
            ask_verified = _ask_matches(ask_pct, ask_quote, ref=ref_d)

    return VerifiedAnalysis(
        terms=verified_terms,
        settlement_ask_pct=ask_pct,
        ask_quote=ask_quote,
        ask_verified=ask_verified,
        stance=analysis.stance,
        readback_response=analysis.readback_response,
        asks_client_private_info=analysis.asks_client_private_info,
        demands_commitment=analysis.demands_commitment,
        hostility=analysis.hostility,
        wants_to_end=analysis.wants_to_end,
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

    return post_verify(analysis, utterance, ref=ref, audit=audit, call_id=call_id)
