"""Pydantic models for every ``/ws/call/{call_id}`` event, plus JSON Schema export.

Server events are what ``app.voice.ws`` sends (``transcript``, ``say``,
``belief``, ``eval``, ``blocked``, ``escalate``, ``latency``, ``audit``,
``phase``, ``agreement``, ``error``, ``turn_done``, ``turn_trace``,
``autoplay_done``, ``speech_dropped``, ``voice_state``, ``agent_sentence_*``); client events are
what it accepts. The web UI's TypeScript types are generated from the
exported schema, so this file is the protocol's single source of truth.
``TurnTrace`` is also the type of ``Orchestrator``'s ``Utterance.trace``.

Fields marked "operator only" are absent (not null) on the ``?view=rep``
stream; ``app.voice.views`` does that filtering, not these models.

Export: ``python -m app.schemas.events --out web/src/types/events.schema.json``
(CI regenerates and fails on diff).
"""

from __future__ import annotations

import argparse
import json
from datetime import date
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


class TraceCushion(_Model):
    """PRIVATE: the firm's minimum client cushion and whether the drafted plan kept it.

    Set only on the turn that drafts a plan (CONFIRM) when a cushion is set. Operator
    only: the rep stream carries no ``turn_trace``.
    """

    min_cushion_cents: int
    kept: bool


class TraceInfeasible(_Model):
    """PRIVATE: why the rep's ask has no payment plan (``feasibility.explain``).

    Set on a rep turn whose ask is above the affordability curve. ``shortfall_cents``
    is what the client's savings lack by ``binding_date`` (None for ``horizon``);
    ``max_segments`` is the creditor's level limit, set only for ``segments``. Client
    finances: operator only. The rep stream carries no ``turn_trace``, and
    ``app.voice.views`` strips this field again by name.
    """

    reason: Literal["horizon", "floors", "segments", "cash_short", "fee_short"]
    bp: int
    binding_date: date | None
    shortfall_cents: int | None
    closest_k: int | None
    max_segments: int | None = None


class Decide(_Model):
    """Policy move: raw ``reason`` code, its ``REASON_TEXT`` key and sentence."""

    intent: Intent
    reason: str | None
    reason_key: str
    reason_text: str
    # The same reason in a few number-free words, for collapsed trace rows. Added
    # by the WS layer at emit time; absent on older frames.
    reason_short: str | None = None


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


class TraceTermAlt(_Model):
    """The payment term a COUNTER_TERMS move asked the rep for (public once said).

    ``field`` is the belief field (``first_payment_date`` ISO date,
    ``min_payment_cents`` cents, ``max_payments`` count).
    """

    field: Literal["first_payment_date", "min_payment_cents", "max_payments"]
    value: int | str


class TraceReader(_Model):
    """Who read the rep's line this turn. Operator only, like the trace.

    ``kind``: ``llm`` (a model; ``provider`` / ``model`` name it), ``code`` (a
    deterministic fast path such as a bare "Correct." to a read-back), or
    ``script`` (oracle NLU: the simulated rep hands over its own reading).
    ``fallback``: the answering model was not the route's first choice;
    ``budget_reached``: a paid target was skipped for today's budget first.
    """

    kind: Literal["llm", "code", "script"]
    provider: str | None = None
    model: str | None = None
    fallback: bool = False
    budget_reached: bool = False
    cache_hit: bool = False


TraceNoteKind = Literal[
    "acked",
    "ack_corrected",
    "ack_disputed",
    "cents_clarify_dropped",
    "amount_held",
    "amount_clarify_dropped",
    "amount_clarify_resolved",
]


class TraceNote(_Model):
    """A step of the turn worth telling in words, from its audit row.

    ``acked``: code acknowledged ``fields`` (and ``total`` cents) before the
    move. ``ack_corrected`` / ``ack_disputed``: the rep corrected or disputed
    an acked ``field`` (``old_value`` → ``new_value``). ``amount_held``: a dollar
    amount (``cents``, ``quote``) was held back for the total-or-per-payment
    question, ``trigger`` says why. ``*_dropped``: a pending question was
    dropped, ``reason`` ``new_terms`` | ``interrupt``. ``amount_clarify_resolved``:
    the rep answered it (``total`` or ``new_value`` = per-payment cents).
    """

    kind: TraceNoteKind
    field: str | None = None
    fields: list[str] | None = None
    old_value: TermValue = None
    new_value: TermValue = None
    cents: int | None = None
    total: int | None = None
    quote: str | None = None
    trigger: str | None = None
    reason: str | None = None


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
    # Set by the WS layer when ``affordability`` is null on a rep turn: the
    # required engine fields still missing (``[]`` = the move did not need the
    # engine). The rep stream carries no ``turn_trace`` at all.
    needs_info: list[str] | None = None
    # Set by the WS layer from the turn's audit rows on rep turns:
    # who read the rep's line, and notable steps (acks, corrections, held-back
    # amounts, dropped questions). Absent on older frames and the opening.
    reader: TraceReader | None = None
    notes: list[TraceNote] | None = None
    decide: Decide
    # Our counter or confirmed bp when this move speaks one (public once said).
    counter_bp: int | None
    # Set on COUNTER_TERMS: the term we asked for (the trace row's key number).
    term_alt: TraceTermAlt | None = None
    # Phase 59: soft client cushion on the plan-drafting turn (PRIVATE, operator only).
    cushion: TraceCushion | None = None
    # Phase 60: why the rep's ask has no plan (PRIVATE, operator only).
    infeasible_reason: TraceInfeasible | None = None
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
    # ``operator``: entered before the call (``start.known_rules`` / firm default), not
    # heard. The rep stream drops terms whose only evidence is the operator's.
    source: Literal["rep", "operator"] = "rep"


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


class ErrorEvent(_Model):
    type: Literal["error"]
    message: str


class TurnDoneEvent(_Model):
    type: Literal["turn_done"]


class SpeechDroppedEvent(_Model):
    """The server dropped these unheard ``say`` sentences: stop them now, never ack them."""

    type: Literal["speech_dropped"]
    ids: list[str]


class VoiceStateEvent(_Model):
    """Pipecat voice path only: what the call is doing now, for the browser's call state.

    ``rep_speaking`` carries Flux's live partial in ``text`` (empty at start of
    turn); ``thinking`` follows a final the agent will answer; ``agent_speaking``
    and ``listening`` follow the agent's audio starting and stopping. ``ended``
    comes once, after the call's last line (handoff or close) finished playing:
    the browser closes the voice link then, not when the call ends on screen.
    """

    type: Literal["voice_state"]
    state: Literal["listening", "rep_speaking", "thinking", "agent_speaking", "ended"]
    text: str = ""


class AgentSentenceStartEvent(_Model):
    """Pipecat voice path only: this sentence's first audio just played; start showing it.

    The browser reveals the words at an estimated pace (Aura-2 gives no word
    timestamps; DECISIONS D25). ``text`` is what the agent is saying aloud,
    already PUBLIC. Acks and fillers are never announced.
    """

    type: Literal["agent_sentence_start"]
    sid: str
    text: str


class AgentSentenceEndEvent(_Model):
    """Pipecat voice path only: this sentence's last audio played (the "heard" point)."""

    type: Literal["agent_sentence_end"]
    sid: str


class AgentSentenceCutEvent(_Model):
    """Pipecat voice path only: the agent was stopped; only part of its reply was spoken.

    ``sid`` is the sentence that was cut (the first dropped one when none had
    started), ``heard_ms`` how much of its audio played, ``heard`` the sentences
    that played whole and ``dropped`` those cut or never played.
    """

    type: Literal["agent_sentence_cut"]
    sid: str
    heard: list[str]
    dropped: list[str]
    heard_ms: int


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
    | ErrorEvent
    | TurnDoneEvent
    | TurnTraceEvent
    | AutoplayDoneEvent
    | SpeechDroppedEvent
    | VoiceStateEvent
    | AgentSentenceStartEvent
    | AgentSentenceEndEvent
    | AgentSentenceCutEvent,
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
    "error",
    "turn_done",
    "turn_trace",
    "autoplay_done",
    "speech_dropped",
    "voice_state",
    "agent_sentence_start",
    "agent_sentence_end",
    "agent_sentence_cut",
)


# ---------------------------------------------------------------- client → server


class KnownTier(_Model):
    """One payment tier: from payment ``from_payment`` (1-based) on, at least ``min_cents``."""

    from_payment: int
    min_cents: int


class KnownRules(_Model):
    """Creditor rules the operator already knows (all optional; absent = not known).

    Validated server-side by ``app.domain.known_rules.parse_known_rules``: cents
    ints, ISO date, tiers that rise and fit the minimum payment and max payments.
    """

    max_payments: int | None = None
    min_payment_cents: int | None = None
    payment_structure: Literal["even", "balloon", "flexible"] | None = None
    min_payment_tiers: list[KnownTier] | None = None
    max_segments: int | None = None
    max_token_pays: int | None = None
    first_payment_date: str | None = None


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
    # This call's minimum client cushion in cents (operator setting, PRIVATE). Absent:
    # the scenario's firm default. 0..500000; the server rejects anything else.
    min_cushion_cents: int | None = None
    # Creditor rules known before the call (operator panel). Absent: the firm's saved
    # rules for this creditor; present (even ``{}``): replaces them for this call.
    known_rules: KnownRules | None = None


class EndEvent(_Model):
    type: Literal["end"]


class TextEvent(_Model):
    type: Literal["text"]
    text: str
    source: str | None = None
    # Ground-truth RepTurn (verified acts), honoured only when the server runs nlu_mode=oracle.
    oracle: dict[str, Any] | None = None


class SentenceDoneEvent(_Model):
    type: Literal["sentence_done"]
    id: str


class BargeInEvent(_Model):
    type: Literal["barge_in"]
    spoken_ids: list[str]


ClientEvent = Annotated[
    StartEvent | EndEvent | TextEvent | SentenceDoneEvent | BargeInEvent,
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
