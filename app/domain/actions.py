"""Shared agent Action types (intent, phase, effects, extra spoken acts).

``sim/`` must not import ``app.agent``, but the creditor simulator needs the
agent's ``Action`` (intent + PUBLIC facts). Policy and NLG keep using these
types from here; ``app.agent.policy`` re-exports for existing callers.

Phase 24b (H3) adds two optional acts that ride on the primary move without
replacing it: ``ack`` (creditor-sourced PUBLIC facts echoed back) and
``answer`` (a number-free talking point for an off-script question). Both are
built by ``app.agent.acts`` after ``decide()``; the sim reacts only to
``intent`` and ``facts``, so the acts never change its reply.
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
    SPEAK_SCHEDULE = "SPEAK_SCHEDULE"
    PROPOSE_WRAP = "PROPOSE_WRAP"
    CLOSE = "CLOSE"
    NO_DEAL_WRAP = "NO_DEAL_WRAP"
    ESCALATE = "ESCALATE"
    # Off-script question answer. Only ever an attached act (``Action.answer``);
    # ``decide()`` never returns it as the primary move.
    ANSWER = "ANSWER"


class Effect(BaseModel):
    """Side effect committed only when spoken sentences are acked."""

    kind: Literal[
        "offer_counter",
        "set_pending_readback",
        "clear_pending_readback",
        "inc_private_ask",
        "inc_commit_demand",
        "record_ask",
        "record_confirm",
        "note_confirm_accepted",
        "inc_confirm_reject",
        "note_assumed_asked",
        "note_clarify",
        "note_question",
        "note_terms_countered",
        "clear_pending_terms_alt",
        "clear_wrap",
        "set_pending_cents_clarify",
        "clear_pending_cents_clarify",
        "set_phase",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


class AnswerAct(BaseModel):
    """Policy-supplied, number-free reply to an off-script question."""

    topic: str
    # Full sentence from ``app.agent.acts.ANSWER_POINTS``; never digits.
    text: str


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
    # When set, NLG uses this template instead of ``TEMPLATES[intent]``.
    template_override: str | None = None
    # H3 acts, spoken before the move: ack facts (``ack_*`` ids, PUBLIC,
    # source="creditor") and an answer. Empty / None = plain one-act turn.
    ack: dict[str, Fact] = Field(default_factory=dict)
    answer: AnswerAct | None = None
