"""Per-call mutable state for the debt-settlement agent.

``CallSession`` holds scenario, belief, negotiation bookkeeping, spoken
history, and the pending ``Action`` whose effects apply only after speech
ack. ``TurnRollback`` is the state from before a rep turn, kept with its
pending speech so a barge that heard nothing can undo the whole turn. It does
not run NLU/policy/NLG — that is ``orchestrator.Orchestrator``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.adapter.engine_adapter import EvalSummary
from app.agent.policy import Agreement, NegotiationState
from app.domain.actions import Action, Phase
from app.domain.belief import BeliefChange, BeliefState
from app.domain.scenario import CallScenario


@dataclass
class Turn:
    """One transcript line (agent or creditor)."""

    role: Literal["agent", "creditor"]
    text: str
    spoken: bool = False
    sentence_id: str | None = None


@dataclass
class TurnRollback:
    """State from before a turn: restored when the rep heard none of its reply.

    ``belief_terms`` (a copy of ``BeliefState.terms``) and ``rep_text`` are
    ``None`` for a turn with no rep line (the opening, a rep hang-up); then
    only the negotiation state and the eager engine results are restored.
    """

    neg: NegotiationState
    last_eval: EvalSummary | None
    agreed_bp: int | None
    history_len: int
    belief_terms: dict[str, Any] | None = None
    rep_text: str | None = None
    last_ack: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingSpeech:
    """Sentences waiting for ``sentence_done`` / barge-in acks.

    ``pending_eval`` / ``pending_agreed_bp`` are engine results for CONFIRM/WRAP
    that commit only on speech ack (barge-in discards them).
    """

    action: Action
    sentence_ids: list[str]
    acked: set[str] = field(default_factory=set)
    pending_eval: EvalSummary | None = None
    pending_agreed_bp: int | None = None
    rollback: TurnRollback | None = None


@dataclass
class CallSession:
    """Mutable state for one outbound settlement call."""

    scenario: CallScenario
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    belief: BeliefState = field(init=False)
    neg: NegotiationState = field(default_factory=NegotiationState)
    history: list[Turn] = field(default_factory=list)
    pending: PendingSpeech | None = None
    # Last engine eval used for CONFIRM / WRAP / CLI verdict display.
    last_eval: EvalSummary | None = None
    agreed_bp: int | None = None
    agreement: Agreement | None = None
    # Highest feasible settlement bp from the last affordability scan (PRIVATE).
    last_max_bp: int | None = None
    # Guard inputs accumulated across the call.
    creditor_numbers: set[tuple[str, int | date]] = field(default_factory=set)
    private_blocklist: set[tuple[str, int | date]] = field(default_factory=set)
    # Recent changes for CLI / tests / voice UI (also audited).
    last_belief_changes: list[BeliefChange] = field(default_factory=list)
    last_blocked: list[dict[str, Any]] = field(default_factory=list)
    # Operator-entered rules seeded at construction (``_seed_known_rules``).
    seeded_changes: list[BeliefChange] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.belief = BeliefState(self.scenario.client)
        self._seed_private_blocklist()
        self.seeded_changes = self._seed_known_rules()

    def _seed_known_rules(self) -> list[BeliefChange]:
        """Operator-entered creditor rules → KNOWN; the orchestrator audits the changes.

        They are creditor terms, so their numbers join ``creditor_numbers``: the agent
        may speak them (read-back, acks) as if the rep had said them.
        """
        changes: list[BeliefChange] = []
        for name, value in self.scenario.known_rules.items():
            changes.append(self.belief.seed_operator(name, value))
            if name == "min_payment_tiers":
                for frm, cents in value:
                    self.creditor_numbers |= {("count", frm), ("ordinal", frm), ("money", cents)}
            elif name == "first_payment_date":
                self.creditor_numbers.add(("date", value))
            elif name == "min_payment_cents":
                self.creditor_numbers.add(("money", value))
            elif isinstance(value, int):
                self.creditor_numbers.add(("count", value))
        return changes

    @property
    def phase(self) -> Phase:
        return self.neg.phase

    @property
    def turn_idx(self) -> int:
        return self.neg.turn_idx

    def last_agent_line(self) -> str:
        """Most recent agent text (for NLU context)."""
        for turn in reversed(self.history):
            if turn.role == "agent":
                return turn.text
        return ""

    def _seed_private_blocklist(self) -> None:
        """Client balances and ledger amounts must never be spoken."""
        client = self.scenario.client
        self.private_blocklist.add(("money", client.draft_amount_cents))
        self.private_blocklist.add(("money", client.current_balance_cents))
        for entry in client.ledger:
            self.private_blocklist.add(("money", entry.amount_cents))
        if self.scenario.min_cushion_cents > 0:  # firm client-protection policy
            self.private_blocklist.add(("money", self.scenario.min_cushion_cents))

    def absorb_private_facts(self, summary: EvalSummary) -> None:
        """Add PRIVATE engine facts to the rendered-guard blocklist."""
        for fact in summary.facts.private().values():
            if isinstance(fact.value, (int, date)):
                self.private_blocklist.add((fact.kind, fact.value))
        # max affordable bp is PRIVATE.
        # Affordability.max_bp is known to the agent but must not be spoken.
