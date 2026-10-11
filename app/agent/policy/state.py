"""Negotiation state and the small value helpers every policy module shares.

``NegotiationState`` is the mutable per-call bookkeeping (asks, counters
offered, ladder stage, loop-guard counts, pending clarifies); the policy only
reads it and requests changes through ``Action.effects``, which the
orchestrator applies on speech ack. ``_Turn`` bundles one ``decide`` call's
inputs. ``Agreement`` / ``draft_agreement`` build the drafted deal from an
engine schedule, ``ask_pct_to_bp`` is the one float → basis-point
conversion, and the ``terms_*`` helpers key the non-price alternatives
already offered. No decisions here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel

from app.adapter.engine_adapter import Affordability
from app.config import Settings
from app.domain.actions import Effect, Phase
from app.domain.belief import BeliefState
from app.domain.facts import Fact
from app.domain.nlu_types import TurnAnalysis
from app.store.audit import AuditLog
from feasibility.engine import ScheduleRow


@dataclass
class NegotiationState:
    """Mutable bargain bookkeeping for one call (ask ladder, counters, flags)."""

    ask_bp: int | None = None
    ask_history: list[int] = field(default_factory=list)
    counters_offered: list[int] = field(default_factory=list)
    private_ask_count: int = 0
    commit_demand_count: int = 0
    pending_readback: str | None = None
    phase: Phase = Phase.OPENING
    turn_idx: int = 0
    # Last CONFIRM fingerprint (ask + rules that shape the schedule).
    last_confirm_key: tuple[Any, ...] | None = None
    # Bp we last proposed in CONFIRM_SCHEDULE (may differ from ask_bp after counters).
    confirmed_bp: int | None = None
    # Confirm key the rep accepted while a READ_BACK preempted the wrap.
    accepted_confirm_key: tuple[Any, ...] | None = None
    confirm_rejects: int = 0
    assumed_asked: set[str] = field(default_factory=set)
    clarify_counts: dict[str, int] = field(default_factory=dict)
    # Non-price alternatives already offered (``field:value`` keys).
    terms_countered: list[str] = field(default_factory=list)
    # Last COUNTER_TERMS awaiting accept/reject: ``{"field", "value"}``
    # (date values stored as ISO strings).
    pending_terms_alt: dict[str, Any] | None = None
    # Bare money reply awaiting dollars-vs-cents disambiguation.
    # ``{"field", "bare", "as_dollars", "as_cents"}`` — amounts in integer cents.
    pending_cents_clarify: dict[str, Any] | None = None
    # Dollar amount awaiting "total or per payment?". Set and cleared
    # by the orchestrator: ``{"cents", "quote", "trigger"}`` plus ``"pct"`` /
    # ``"pct_quote"`` when a disagreeing % ask was held back.
    pending_amount_clarify: dict[str, Any] | None = None
    # Price ladder. Rep's ask when our last counter was spoken (did
    # they move since?), hold / step stage (0 none, 1 held, 2–3 equal steps),
    # the rep's firm ask when we made our final counter, and the turns that
    # spoke a COUNTER (holds included) for the ``max_counters`` cap.
    ask_at_last_counter: int | None = None
    hold_stage: int = 0
    final_counter_ask: int | None = None
    counter_turns: list[int] = field(default_factory=list)
    # Loop guard: times each question was asked (``question_key``) and
    # rep turns in a row that added nothing (``rep_turn_progress``).
    question_counts: dict[str, int] = field(default_factory=dict)
    no_progress_turns: int = 0


def terms_counter_key(field: str, value: Any) -> str:
    """Stable key for ``terms_countered`` (dates as ISO)."""
    if isinstance(value, date):
        return f"{field}:{value.isoformat()}"
    return f"{field}:{value}"


def field_already_countered(terms_countered: list[str], field: str) -> bool:
    """True when any prior COUNTER_TERMS was for ``field``.

    Used for ``first_payment_date`` (one try). Min / max may progress to a
    better value when a prior alt left the ask unreachable.
    """
    prefix = f"{field}:"
    return any(k.startswith(prefix) for k in terms_countered)


def parse_pending_terms_value(pending: dict[str, Any]) -> Any:
    """Deserialize pending alt value (ISO date → ``date``)."""
    field = str(pending["field"])
    raw = pending["value"]
    if field == "first_payment_date":
        return date.fromisoformat(str(raw))
    if field in ("min_payment_cents", "max_payments"):
        return int(raw)
    return raw


class Agreement(BaseModel):
    creditor: str
    bp: int
    offer_total: int
    rows: list[dict[str, Any]]
    assumed_fields: list[str]
    status: Literal["pending_client_approval"] = "pending_client_approval"


def ask_pct_to_bp(pct: float) -> int:
    """Convert settlement ask percent points (45.0 → 4500 bp)."""
    return int(
        (Decimal(str(pct)) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP)
    )


def draft_agreement(
    *,
    creditor: str,
    bp: int,
    offer_total: int,
    rows: list[ScheduleRow] | list[dict[str, Any]],
    assumed_fields: list[str],
    audit: AuditLog | None = None,
    call_id: str | None = None,
) -> Agreement:
    """Build a pending client-approval agreement; never a spoken commitment."""
    serialized: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            serialized.append(row)
        else:
            serialized.append(
                {
                    "date": row.date.isoformat(),
                    "creditor_payment_cents": row.creditor_payment_cents,
                    "program_fee_cents": row.program_fee_cents,
                    "bank_fee_cents": row.bank_fee_cents,
                    "balance_cents": row.balance_cents,
                }
            )
    agreement = Agreement(
        creditor=creditor,
        bp=bp,
        offer_total=offer_total,
        rows=serialized,
        assumed_fields=list(assumed_fields),
        status="pending_client_approval",
    )
    if audit is not None and call_id is not None:
        audit.append(
            call_id,
            "agent",
            "agreement_drafted",
            agreement.model_dump(),
        )
    return agreement


@dataclass(frozen=True)
class _Turn:
    """Inputs of one ``decide`` call after the prelude (ask recorded, rejected alt cleared)."""

    belief: BeliefState
    neg: NegotiationState
    analysis: TurnAnalysis
    afford: Affordability | None
    cfg: Settings
    ask_bp: int | None
    effects: list[Effect]
    rescue_within_guardrail: bool
    confirm_facts: dict[str, Fact] | None
    counter_offer_total_cents: int | None
    term_alt: tuple[str, Any] | None
    accepted_term_alt: bool
