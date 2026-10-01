"""Shared agent Action types (intent, phase, effects).

``sim/`` must not import ``app.agent``, but the creditor simulator needs the
agent's ``Action`` (intent + PUBLIC facts). Policy and NLG keep using these
types from here; ``app.agent.policy`` re-exports for existing callers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.facts import Fact


class Phase(StrEnum):
    OPENING = "OPENING"
    DISCOVERY = "DISCOVERY"
    NEGOTIATE = "NEGOTIATE"
    CONFIRM = "CONFIRM"
    WRAP = "WRAP"
    ESCALATE = "ESCALATE"
    END = "END"


class Intent(StrEnum):
    OPENING = "OPENING"
    ASK = "ASK"
    ASK_SETTLEMENT = "ASK_SETTLEMENT"
    READ_BACK = "READ_BACK"
    CLARIFY = "CLARIFY"
    REFUSE_PRIVATE = "REFUSE_PRIVATE"
    REFUSE_COMMIT = "REFUSE_COMMIT"
    COUNTER = "COUNTER"
    COUNTER_TERMS = "COUNTER_TERMS"
    CONFIRM_SCHEDULE = "CONFIRM_SCHEDULE"
    PROPOSE_WRAP = "PROPOSE_WRAP"
    NO_DEAL_WRAP = "NO_DEAL_WRAP"
    ESCALATE = "ESCALATE"


class Effect(BaseModel):
    """Side effect committed only when spoken sentences are acked."""

    kind: Literal[
        "offer_counter",
        "set_pending_readback",
        "clear_pending_readback",
        "inc_private_ask",
        "inc_commit_demand",
        "record_ask",
        "inc_reject_at_max",
        "record_confirm",
        "inc_confirm_reject",
        "note_assumed_asked",
        "note_clarify",
        "note_terms_countered",
        "clear_pending_terms_alt",
        "set_phase",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


class Action(BaseModel):
    """Next agent move: intent, PUBLIC facts, text slots, and deferred effects."""

    intent: Intent
    facts: dict[str, Fact] = Field(default_factory=dict)
    # Non-numeric fills (ask copy, escalate reason, firm name). Never digits.
    text_slots: dict[str, str] = Field(default_factory=dict)
    required: set[str] = Field(default_factory=set)
    effects: list[Effect] = Field(default_factory=list)
    next_phase: Phase
    # Optional free-form reason for ESCALATE / tests.
    reason: str | None = None
