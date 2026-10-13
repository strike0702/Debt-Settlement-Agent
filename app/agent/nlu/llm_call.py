"""``analyze``: the NLU entry point for one rep turn.

Order: oracle mode first (a caller-supplied ``TurnAnalysis``, for the sim and
tests); else the ``fast_paths`` that need no model
(plain yes/no to a read-back, a short pick between the two values of an open
clarify, a bare answer to the field we just asked); else one JSON ``nlu`` call
through ``app.llm.client`` (retried once on a parse / validation failure, with
no broad ``except``), then ``verify.post_verify``. Dollar amounts are resolved
later by the orchestrator (``amounts.resolve_amounts``), once the balance is
known. Does not update belief.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Protocol

from pydantic import ValidationError

from app.agent.nlu.cues import _repair_dispositions
from app.agent.nlu.fast_paths import (
    try_fast_field_answer,
    try_fast_readback,
    try_resolve_disputed_value,
)
from app.agent.nlu.models import VerifiedAnalysis
from app.agent.nlu.parse import _parse_analysis
from app.agent.nlu.verify import post_verify
from app.agent.nlu_types import TurnAnalysis
from app.config import Settings, get_settings
from app.llm.client import LLMUnavailable
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
    disputed: tuple[str, Any, Any] | None = None,
) -> VerifiedAnalysis:
    """Run NLU (or oracle) then post-verify. Empty analysis on double validation fail.

    ``disputed``: ``(field, old, new)`` of a CONTRADICTED term we are asking
    about; a short reply that picks one side skips the LLM.
    """
    cfg = settings or get_settings()
    empty = TurnAnalysis(stance="other")

    if cfg.nlu_mode == "oracle":
        if oracle is None:
            raise ValueError("nlu_mode=oracle requires oracle=TurnAnalysis")
        return post_verify(
            oracle,
            utterance,
            ref=ref,
            audit=audit,
            call_id=call_id,
            last_agent_line=last_agent_line,
        )

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

    # Short pick between the two values of an open clarify — skip the LLM.
    if disputed is not None:
        picked = try_resolve_disputed_value(utterance, *disputed, ref=ref)
        if picked is not None:
            if audit is not None and call_id is not None:
                audit.append(
                    call_id,
                    "nlu",
                    "fast_field_answer",
                    {
                        "field": picked.terms[0].field,
                        "value": picked.terms[0].value,
                        "utterance": utterance[:80],
                        "disputed": True,
                    },
                )
            return picked

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

    verified = post_verify(
        analysis,
        utterance,
        ref=ref,
        audit=audit,
        call_id=call_id,
        last_agent_line=last_agent_line,
    )

    # When a sim/oracle disposition is supplied under live NLU, keep verified
    # terms/ask from the LLM but overlay stance flags from ground truth so
    # phrasing artifacts cannot false-escalate.
    if oracle is not None:
        has_terms = bool(verified.terms) or verified.settlement_ask_pct is not None
        verified = verified.model_copy(
            update=_repair_dispositions(oracle, utterance, has_terms=has_terms)
        )
    return verified
