"""TurnAnalysis / ExtractedTerm — shared by NLU, policy, and the sim.

The simulator emits ground-truth ``TurnAnalysis`` for oracle NLU and must not
import ``app.agent``. Agent NLU re-exports these from ``app.agent.nlu_types``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
    value: int | str | dict | list
    quote: str  # verbatim span from the utterance
    hedged: bool = False


class TurnAnalysis(BaseModel):
    terms: list[ExtractedTerm] = Field(default_factory=list)
    settlement_ask_pct: float | None = None  # percent points, e.g. 45.0 = 45%
    ask_quote: str | None = None
    stance: Literal[
        "offer", "counter", "accept", "reject", "stall", "info", "question", "other"
    ] = "other"
    readback_response: Literal["confirm", "deny"] | None = None
    asks_client_private_info: bool = False
    demands_commitment: bool = False
    hostility: float = 0.0
    wants_to_end: bool = False
