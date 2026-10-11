"""NLU result models: what the rest of the agent reads after a rep turn.

``VerifiedTerm`` / ``VerifiedAnalysis`` are the post-verified form of the LLM's
``TurnAnalysis`` (``app.agent.nlu_types``): each term carries ``verified`` and
the analysis carries every side-channel flag after the code-side repairs.
``DroppedTerm`` records what verification threw away, for the decision trace
only (policy never reads it). No logic beyond ``to_turn_analysis``; built by
``verify.post_verify``, ``fast_paths`` and ``amounts``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.nlu_types import ExtractedTerm, TurnAnalysis


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
    # Dollar-total ask: verified against its quote here; converted to
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
    # Off-script question; answered by ``app.agent.acts``, not policy.
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
