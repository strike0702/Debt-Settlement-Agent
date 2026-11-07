"""Call context for the acts NLU prompt: a PUBLIC summary of where the call stands.

The orchestrator builds one ``CallContext`` per rep turn from the belief and
negotiation state (``build_call_context``) and passes it to ``analyze``; with
``NLU_SCHEMA=acts`` it goes into the prompt as a compact JSON block, so the
model knows a bare "seventy three" answers a price and "that's all" closes a
list. It carries only what both sides already said on the call: the phase,
what the agent is waiting for, our last spoken offer, the rep's asks, the list
being collected and the rules the rep stated. Never the client's maximum, the
accept line, balances, fees, or any PRIVATE fact (a test scans every prompt on
every scenario for the private blocklist). Not used by the flat schema.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.policy.state import NegotiationState
from app.domain.belief import BeliefState, TermStatus

Awaiting = Literal["field", "price_response", "readback", "list_item", "none"]

# Belief statuses whose value the rep said (ASSUMED values are our defaults).
_REP_STATED = (TermStatus.KNOWN, TermStatus.TENTATIVE, TermStatus.CONTRADICTED)
_PRICE_PHASES = ("NEGOTIATE", "CONFIRM")


class ListState(BaseModel):
    """A list the rep is giving item by item (only payment tiers today)."""

    name: Literal["tiers"] = "tiers"
    have: list[tuple[int, int]] = Field(default_factory=list)  # (from_payment, min_cents)
    expected: int | None = None


class CallContext(BaseModel):
    """PUBLIC call state for the acts NLU prompt (percent points, cents)."""

    phase: str = "OPENING"
    awaiting: Awaiting = "none"
    awaiting_field: str | None = None
    our_last_offer_pct: float | None = None
    rep_last_ask_pct: float | None = None
    rep_final_ask_pct: float | None = None
    collecting: ListState | None = None
    # field → {"value", "source"}: ``rep`` (the rep said it) or ``operator``
    # (seeded from the firm's records and stated by the agent; Phase 69).
    known_rules: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def prompt_json(self) -> str:
        """Compact JSON for the prompt: unset keys dropped, dates ISO."""
        data = self.model_dump(mode="json", exclude_none=True)
        if not data.get("known_rules"):
            data.pop("known_rules", None)
        return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _pct(bp: int | None) -> float | None:
    return None if bp is None else bp / 100


def _awaiting(neg: NegotiationState) -> tuple[Awaiting, str | None]:
    if neg.pending_readback is not None:
        return "readback", neg.pending_readback
    if neg.tier_draft is not None:
        return "list_item", "min_payment_tiers"
    key = neg.last_question_key or ""
    if key.startswith("ASK_SETTLEMENT"):
        return "price_response", None
    if key.startswith("ASK:"):
        return "field", key.split(":")[1] or None
    if neg.phase.value in _PRICE_PHASES and (neg.counters_offered or neg.confirmed_bp):
        return "price_response", None
    return "none", None


def build_call_context(belief: BeliefState, neg: NegotiationState) -> CallContext:
    """The PUBLIC context for this turn; reads only rep-stated rules and spoken moves."""
    awaiting, field = _awaiting(neg)
    ours = neg.counters_offered[-1] if neg.counters_offered else neg.confirmed_bp
    rules: dict[str, dict[str, Any]] = {}
    for name, term in belief.terms.items():
        if term.status in _REP_STATED and term.value is not None:
            value = term.value
            # Phase 69 seeds operator rules with evidence ``source="operator"``; a
            # rep statement adds rep evidence (and replaces the value), so only a
            # rule with operator evidence alone is still the firm's note.
            source = (
                "operator"
                if term.evidence and all(e.source == "operator" for e in term.evidence)
                else "rep"
            )
            rules[name] = {
                "value": value.isoformat() if isinstance(value, date) else value,
                "source": source,
            }
    collecting = None
    if neg.tier_draft is not None:
        collecting = ListState(have=list(neg.tier_draft), expected=neg.tier_expected)
    return CallContext(
        phase=neg.phase.value,
        awaiting=awaiting,
        awaiting_field=field,
        our_last_offer_pct=_pct(ours),
        rep_last_ask_pct=_pct(neg.ask_bp),
        rep_final_ask_pct=_pct(neg.final_counter_ask),
        collecting=collecting,
        known_rules=rules,
    )
