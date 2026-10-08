"""TurnAnalysis / ExtractedTerm — shared by NLU, policy, and the sim.

The simulator emits ground-truth ``TurnAnalysis`` for oracle NLU and must not
import ``app.agent``. Agent NLU re-exports these from ``app.agent.nlu_types``.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# Off-script question topics (Phase 24b). Anything else the rep asks is "other".
QuestionTopic = Literal["why_not_higher", "next_steps", "who_approves", "timeline", "other"]
QUESTION_TOPICS: tuple[str, ...] = (
    "why_not_higher",
    "next_steps",
    "who_approves",
    "timeline",
    "other",
)


class ExtractedTerm(BaseModel):
    field: Literal[
        "max_payments",
        "min_payment_cents",
        "payment_structure",
        "first_payment_date",
        "max_segments",
        "max_token_pays",
        "min_payment_tiers",
    ]
    value: int | str | date | dict | list
    quote: str  # verbatim span from the utterance
    hedged: bool = False


class TurnAnalysis(BaseModel):
    terms: list[ExtractedTerm] = Field(default_factory=list)
    settlement_ask_pct: float | None = None  # percent points, e.g. 45.0 = 45%
    ask_quote: str | None = None
    # Settlement ask stated as a dollar total (Phase 39), integer cents. Code,
    # never the LLM, converts it to basis points of the account balance.
    settlement_ask_total_cents: int | None = None
    ask_total_quote: str | None = None
    # A dollar amount that may be the total to settle or the minimum per payment
    # (Phase 39). The agent asks which one instead of guessing.
    amount_ambiguous_cents: int | None = None
    amount_ambiguous_quote: str | None = None
    stance: Literal[
        "offer", "counter", "accept", "reject", "stall", "info", "question", "other"
    ] = "other"
    readback_response: Literal["confirm", "deny"] | None = None
    asks_client_private_info: bool = False
    demands_commitment: bool = False
    hostility: float = 0.0
    wants_to_end: bool = False
    asks_for_schedule: bool = False
    # True when the rep says the number is final / their floor / cannot go lower.
    firm: bool = False
    # Tiers phrased as "first N payments": dropped by NLU, policy asks to rephrase.
    tiers_ambiguous: bool = False
    # Off-script process question (not a term, price, schedule or private ask).
    # NLU-sourced only; ``decide()`` ignores it, ``app.agent.acts`` answers it.
    asks_question: bool = False
    question_topic: QuestionTopic | None = None
