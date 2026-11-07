"""``analyze``: the NLU entry point for one rep turn.

Order: oracle mode first (a caller-supplied ``RepTurn``, for the sim and
tests); else the ``fast_paths`` that need no model (plain yes/no to a
read-back, a short pick between the two values of an open clarify, a bare
answer to the field we just asked); else one JSON ``nlu`` call through
``app.llm.client`` with the acts prompt and the PUBLIC ``CallContext`` (no
retry: an invalid reply keeps its valid acts, else is one ``unclear`` act),
then ``acts.verify_acts``. Oracle acts go through the same ``verify_acts``.
Dollar amounts are resolved later by the orchestrator
(``amounts.resolve_amounts``), once the balance is known. Does not update
belief.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from app.agent.nlu.acts import parse_acts, verify_acts
from app.agent.nlu.context import CallContext
from app.agent.nlu.cues import repair_acts
from app.agent.nlu.fast_paths import (
    try_fast_field_answer,
    try_fast_readback,
    try_resolve_disputed_value,
)
from app.agent.nlu.models import VerifiedAnalysis
from app.config import Settings, get_settings
from app.domain.nlu_types import EndCallAct, PriceAskAct, RepTurn
from app.llm.prompts import nlu_acts_messages
from app.store.audit import AuditLog

# Acts replies are short (a few acts); the cap only bounds a runaway reply.
_NLU_ACTS_MAX_TOKENS = 600


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
    oracle: RepTurn | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
    ref: date | None = None,
    disputed: tuple[str, Any, Any] | None = None,
    context: CallContext | None = None,
) -> VerifiedAnalysis:
    """Run NLU (or verify the oracle's acts) for one rep line.

    ``disputed``: ``(field, old, new)`` of a CONTRADICTED term we are asking
    about; a short reply that picks one side skips the LLM.
    """
    cfg = settings or get_settings()

    if cfg.nlu_mode == "oracle":
        if oracle is None:
            raise ValueError("nlu_mode=oracle requires oracle=RepTurn")
        return verify_acts(
            oracle.acts,
            utterance,
            context=context,
            last_agent_line=last_agent_line,
            pending_readback=pending_readback,
            ref=ref,
            audit=audit,
            call_id=call_id,
            oracle=True,
            amount_ambiguous=(
                (oracle.amount_ambiguous_cents, oracle.amount_ambiguous_quote)
                if oracle.amount_ambiguous_cents is not None
                else None
            ),
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

    ctx = context or CallContext()
    messages = nlu_acts_messages(utterance, last_agent_line, ctx.prompt_json(), ref=ref)
    text = await llm.chat_text("nlu", messages, _NLU_ACTS_MAX_TOKENS, json_mode=True)
    acts, error = parse_acts(text)
    if error is not None and audit is not None and call_id is not None:
        audit.append(call_id, "nlu", "nlu_acts_invalid", {"error": error, "reply": text[:300]})
    verified = verify_acts(
        acts,
        utterance,
        context=ctx,
        last_agent_line=last_agent_line,
        pending_readback=pending_readback,
        ref=ref,
        audit=audit,
        call_id=call_id,
    )
    if oracle is not None:
        verified = overlay_oracle_acts(verified, oracle, utterance, context=ctx)
    return verified




def overlay_oracle_acts(
    verified: VerifiedAnalysis, oracle: RepTurn, utterance: str, *, context: CallContext
) -> VerifiedAnalysis:
    """Live NLU with a sim's ground truth: keep the model's rules, price,
    announcements, list end and off-script questions; take the oracle's
    dispositions (accept / reject, read-back reply, private ask, commitment
    demand, hostility, closing, firmness), repaired by the same cues, so
    phrasing artifacts cannot false-escalate.
    """
    from_oracle = [
        PriceAskAct(type="price_ask", firmness="final", quote=a.quote)
        if isinstance(a, PriceAskAct)
        else a
        for a in oracle.acts
        if a.type in _DISPOSITIONS or (isinstance(a, PriceAskAct) and a.firmness == "final")
    ]
    from_model = [a for a in verified.acts if a.type in _MODEL_KEEPS]
    intent = [*from_model, *from_oracle]
    has_terms = bool(verified.terms) or verified.settlement_ask_pct is not None
    acts, _ = repair_acts(
        intent,
        utterance,
        has_terms=has_terms,
        given_fields={t.field for t in verified.terms},
        collecting=context.collecting is not None,
        model_ended=any(isinstance(a, EndCallAct) for a in from_oracle),
    )
    return verified.model_copy(update={"acts": acts})


_DISPOSITIONS = frozenset(
    {"respond_to_offer", "readback_reply", "ask_private_info", "demand_commitment",
     "hostility", "end_call"}
)  # fmt: skip
_MODEL_KEEPS = frozenset({"announce_rules", "list_done", "question", "stall", "backchannel"})
