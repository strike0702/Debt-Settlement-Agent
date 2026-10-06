"""Pydantic models for every ``/ws/call/{call_id}`` event, plus JSON Schema export.

Server events are what ``app.voice.ws`` sends (``transcript``, ``say``,
``belief``, ``eval``, ``blocked``, ``escalate``, ``latency``, ``audit``,
``phase``, ``agreement``, ``stt_error``, ``error``, ``turn_done``,
``turn_trace``, ``autoplay_done``); client events are what it accepts. The web
UI's TypeScript types are generated from the exported schema, so this file is
the protocol's single source of truth. ``TurnTrace`` is also the type of
``Orchestrator``'s ``Utterance.trace``.

Fields marked "operator only" are absent (not null) on the ``?view=rep``
stream; ``app.voice.views`` does that filtering, not these models.

Export: ``python -m app.schemas.events --out web/src/types/events.schema.json``
(CI regenerates and fails on diff).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.actions import Intent, Phase

View = Literal["rep", "operator"]
VIEWS: tuple[View, ...] = ("rep", "operator")

TermStatusName = Literal["UNKNOWN", "TENTATIVE", "KNOWN", "CONTRADICTED", "ASSUMED"]
Stance = Literal["offer", "counter", "accept", "reject", "stall", "info", "question", "other"]
# Belief / term values on the wire: ints (cents, counts), enum strings, ISO
# dates, or tiers as ``[[from_payment, min_cents], ...]``.
TermValue = int | float | str | list[list[int]] | None


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- trace parts


class TraceTerm(_Model):
    """A term NLU extracted and ``post_verify`` kept; ``quote`` is a span of the rep line."""

    field: str
    value: TermValue
    quote: str
    verified: bool
    hedged: bool


class DroppedTerm(_Model):
    """A term ``post_verify`` dropped; ``reason`` is the audit event minus ``nlu_``."""

    field: str
    value: Any = None
    reason: str
    quote: str | None = None


class TraceBeliefChange(_Model):
    field: str
    old_value: TermValue
    new_value: TermValue
    old_status: TermStatusName
    new_status: TermStatusName
    turn: int
    quote: str | None = None


class CurvePoint(_Model):
    bp: int
    feasible: bool


class Affordability(_Model):
    """PRIVATE engine view: highest feasible bp and the 1–100% curve. Operator only."""

    max_bp: int | None
    curve: list[CurvePoint]


class Decide(_Model):
    """Policy move: raw ``reason`` code, its ``REASON_TEXT`` key and sentence."""

    intent: Intent
    reason: str | None
    reason_key: str
    reason_text: str


class GuardResult(_Model):
    stage: Literal["template", "unfilled", "rendered"]
    ok: bool
    reason: str | None = None
    # Operator only: the offending tokens can be private figures.
    offending: list[str] | None = None


class NlgTrace(_Model):
    """How the line was phrased: mode, chosen template, guard verdicts, fallback."""

    mode: Literal["template", "bank", "llm"]
    source: Literal["default", "override", "bank", "llm"]
    template: str
    guards: list[GuardResult]
    fallback_used: bool
    fallback_reason: str | None = None


class SpokenSentence(_Model):
    id: str
    text: str


class TurnTrace(_Model):
    """Everything one agent turn did, in pipeline order (``Utterance.trace``)."""

    turn: int
    creditor_text: str | None
    stance: Stance | None
    ask_bp: int | None
    ask_quote: str | None
    terms: list[TraceTerm]
    dropped: list[DroppedTerm]
    belief_changes: list[TraceBeliefChange]
    affordability: Affordability | None = None
    decide: Decide
    # Our counter or confirmed bp when this move speaks one (public once said).
    counter_bp: int | None
    nlg: NlgTrace
    spoken: list[SpokenSentence]
    timings: dict[str, float | None]


# ---------------------------------------------------------------- server → client


class TranscriptEvent(_Model):
    type: Literal["transcript"]
    role: Literal["creditor", "agent"]
    text: str
    spoken: bool
    sentence_id: str | None
    blocked: bool


class SayEvent(_Model):
    type: Literal["say"]
    id: str
    text: str


class Evidence(_Model):
    turn: int
    quote: str


class BeliefTerm(_Model):
    field: str
    value: TermValue
    status: TermStatusName
    evidence: list[Evidence]
    history: list[TermValue]


class BeliefEvent(_Model):
    type: Literal["belief"]
    terms: list[BeliefTerm]


class ScheduleRow(_Model):
    date: str
    creditor_payment_cents: int
    # Operator only (firm fees and the client's savings balance).
    program_fee_cents: int | None = None
    bank_fee_cents: int | None = None
    balance_cents: int | None = None


class FundsOption(_Model):
    amount_cents: int | None
    within_guardrail: bool
    reason: str | None
    date: str | None
    num_drafts: int | None


class AdditionalFunds(_Model):
    lump_sum: FundsOption
    monthly_increment: FundsOption


class EvalEvent(_Model):
    type: Literal["eval"]
    feasible: bool
    shape: str | None
    offer_total_cents: int | None
    assumed_fields: list[str]
    agreed_bp: int | None
    rows: list[ScheduleRow] | None
    # Operator only.
    program_fee_cents: int | None = None
    additional_funds: AdditionalFunds | None = None
    max_bp: int | None = None


class BlockedEvent(_Model):
    type: Literal["blocked"]
    stage: Literal["template", "unfilled", "rendered"]
    reason: str
    # Operator only.
    offending: list[str] | None = None


class EscalateEvent(_Model):
    type: Literal["escalate"]
    reason: str | None
    escalate_reason: str | None


class LatencyEvent(_Model):
    type: Literal["latency"]
    turn: int
    stt_ms: float | None
    nlu_ms: float | None
    engine_ms: float | None
    policy_ms: float | None
    nlg_ms: float | None
    queue_ms: float | None
    server_total_ms: float | None


class AuditEvent(_Model):
    type: Literal["audit"]
    id: int
    ts: str
    actor: str
    event: str
    payload: Any
    # True on rows the rep stream never receives (engine, fees, guard hits, …).
    private: bool = False


class PhaseEvent(_Model):
    type: Literal["phase"]
    phase: Phase
    intent: Intent | None
    turn: int


class AgreementEvent(_Model):
    type: Literal["agreement"]
    creditor: str
    bp: int
    offer_total: int
    status: Literal["pending_client_approval"]
    assumed_fields: list[str]
    rows: list[ScheduleRow]


class SttErrorEvent(_Model):
    type: Literal["stt_error"]
    message: str


class ErrorEvent(_Model):
    type: Literal["error"]
    message: str


class TurnDoneEvent(_Model):
    type: Literal["turn_done"]


class TurnTraceEvent(TurnTrace):
    type: Literal["turn_trace"]


AutoplayOutcome = Literal["deal", "no_deal", "escalate", "incomplete"]


class AutoplayDoneEvent(_Model):
    type: Literal["autoplay_done"]
    outcome: AutoplayOutcome
    phase: Phase
    final_intent: Intent
    turns: int


ServerEvent = Annotated[
    TranscriptEvent
    | SayEvent
    | BeliefEvent
    | EvalEvent
    | BlockedEvent
    | EscalateEvent
    | LatencyEvent
    | AuditEvent
    | PhaseEvent
    | AgreementEvent
    | SttErrorEvent
    | ErrorEvent
    | TurnDoneEvent
    | TurnTraceEvent
    | AutoplayDoneEvent,
    Field(discriminator="type"),
]
SERVER_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ServerEvent)
SERVER_EVENT_TYPES: tuple[str, ...] = (
    "transcript",
    "say",
    "belief",
    "eval",
    "blocked",
    "escalate",
    "latency",
    "audit",
    "phase",
    "agreement",
    "stt_error",
    "error",
    "turn_done",
    "turn_trace",
    "autoplay_done",
)


# ---------------------------------------------------------------- client → server


class StartEvent(_Model):
    """Start a call. ``autoplay`` lets the sim creditor drive it (curated ids only)."""

    type: Literal["start"]
    scenario_id: str | None = None
    scenario_payload: dict[str, Any] | None = None
    # Legacy fixture path (tests only).
    scenario: str | None = None
    autoplay: bool = False
    # Pause between autoplay turns; default 1200, clamped to 0–10000.
    autoplay_pause_ms: int | None = None


class EndEvent(_Model):
    type: Literal["end"]


class TextEvent(_Model):
    type: Literal["text"]
    text: str
    source: str | None = None
    # Ground-truth TurnAnalysis, honoured only when the server runs nlu_mode=oracle.
    oracle: dict[str, Any] | None = None


class SentenceDoneEvent(_Model):
    type: Literal["sentence_done"]
    id: str


class BargeInEvent(_Model):
    type: Literal["barge_in"]
    spoken_ids: list[str]


class TimingEvent(_Model):
    type: Literal["timing"]
    turn: int | None = None
    vad_end_to_first_audio_ms: float | None = None


ClientEvent = Annotated[
    StartEvent | EndEvent | TextEvent | SentenceDoneEvent | BargeInEvent | TimingEvent,
    Field(discriminator="type"),
]


class WsProtocol(BaseModel):
    """Export root: both directions of ``/ws/call/{call_id}?view=rep|operator``."""

    server_event: ServerEvent = Field(alias="ServerEvent")
    client_event: ClientEvent = Field(alias="ClientEvent")
    view: View = Field(alias="View")


def export_schema() -> dict[str, Any]:
    """JSON Schema for ``WsProtocol`` (deterministic)."""
    return WsProtocol.model_json_schema(by_alias=True)


def schema_text() -> str:
    """The exact file content written by the CLI (sorted keys, trailing newline)."""
    return json.dumps(export_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI: write the schema to ``--out``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(schema_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
