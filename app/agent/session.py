"""Per-call mutable state for the debt-settlement agent.

``CallSession`` holds scenario, belief, negotiation bookkeeping, spoken
history, and the pending ``Action`` whose effects apply only after speech
ack. It does not run NLU/policy/NLG — that is ``orchestrator.Orchestrator``.
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
class PendingSpeech:
    """Sentences waiting for ``sentence_done`` / barge-in acks."""

    action: Action
    sentence_ids: list[str]
    acked: set[str] = field(default_factory=set)


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
    # Guard inputs accumulated across the call.
    creditor_numbers: set[tuple[str, int | date]] = field(default_factory=set)
    private_blocklist: set[tuple[str, int | date]] = field(default_factory=set)
    # Recent changes for CLI / tests (also audited).
    last_belief_changes: list[BeliefChange] = field(default_factory=list)
    last_blocked: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.belief = BeliefState(self.scenario.client)
        self._seed_private_blocklist()

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

    def absorb_private_facts(self, summary: EvalSummary) -> None:
        """Add PRIVATE engine facts to the rendered-guard blocklist."""
        for fact in summary.facts.private().values():
            if isinstance(fact.value, (int, date)):
                self.private_blocklist.add((fact.kind, fact.value))
        # max affordable bp is PRIVATE (PLAN §4.3).
        # Affordability.max_bp is known to the agent but must not be spoken.
