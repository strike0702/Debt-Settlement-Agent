"""The interface every A/B arm implements, plus the policy-arm adapter.

``eval.run_eval.run_one_scenario`` drives a call through ``AgentUnderTest``
only: ``start`` → (creditor reply → ``on_creditor_text``)* with auto-ack, and
reads ``agent.session`` (a ``CallSession``) for every metric. Each method
returns an ``app.agent.orchestrator.Utterance`` (``.action``, ``.sentences``,
``.timings``, ``.agreement``), so ``sim.creditor.CreditorPolicy.respond`` and
the leak / unverified / validator scans work unchanged for every arm.

``PolicyAgent`` (arm A) wraps the production ``Orchestrator`` without changing
it; ``--agent policy`` output is byte-identical to the pre-24a runner. The LLM
arms live in ``react_agent`` / ``llm_only_agent`` and are eval-only.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, get_args, runtime_checkable

from app.agent.nlu_types import TurnAnalysis
from app.agent.orchestrator import Orchestrator, Utterance
from app.agent.policy import Agreement
from app.agent.session import CallSession
from app.config import Settings
from app.store.audit import AuditLog

AgentName = Literal["policy", "react", "llm_only"]
AGENT_NAMES: tuple[str, ...] = get_args(AgentName)


@runtime_checkable
class AgentUnderTest(Protocol):
    """One negotiating agent driven turn by turn by the eval runner."""

    session: CallSession

    async def start(self) -> Utterance:
        """Speak the opening line."""
        ...

    async def on_creditor_text(
        self,
        text: str,
        timings: dict[str, float] | None = None,
        *,
        oracle: TurnAnalysis | None = None,
    ) -> Utterance:
        """React to one creditor utterance; ``oracle`` is the sim ground truth for NLU."""
        ...

    async def on_sentence_done(self, ids: list[str] | set[str]) -> Agreement | None:
        """Ack spoken sentence ids (the runner auto-acks, so this is usually a no-op)."""
        ...


class PolicyAgent:
    """Arm A: the production ``Orchestrator`` (code policy + NLG), unchanged."""

    name: AgentName = "policy"

    def __init__(
        self,
        session: CallSession,
        *,
        llm: Any | None,
        settings: Settings,
        audit: AuditLog | None,
    ) -> None:
        self.session = session
        self.orchestrator = Orchestrator(
            session, llm=llm, settings=settings, audit=audit, auto_ack=True
        )

    async def start(self) -> Utterance:
        """Delegate to ``Orchestrator.start``."""
        return await self.orchestrator.start()

    async def on_creditor_text(
        self,
        text: str,
        timings: dict[str, float] | None = None,
        *,
        oracle: TurnAnalysis | None = None,
    ) -> Utterance:
        """Delegate to ``Orchestrator.on_creditor_text``."""
        return await self.orchestrator.on_creditor_text(text, timings, oracle=oracle)

    async def on_sentence_done(self, ids: list[str] | set[str]) -> Agreement | None:
        """Delegate to ``Orchestrator.on_sentence_done``."""
        return await self.orchestrator.on_sentence_done(ids)


def make_agent(
    name: str,
    session: CallSession,
    *,
    llm: Any | None,
    settings: Settings,
    audit: AuditLog | None,
) -> AgentUnderTest:
    """Build the arm named ``name`` (``policy`` | ``react`` | ``llm_only``)."""
    if name == "policy":
        return PolicyAgent(session, llm=llm, settings=settings, audit=audit)
    # Imported lazily so the CI policy path never loads the LLM arms.
    if name == "react":
        from eval.agents.react_agent import ReactAgent

        return ReactAgent(session, llm=llm, settings=settings, audit=audit)
    if name == "llm_only":
        from eval.agents.llm_only_agent import LLMOnlyAgent

        return LLMOnlyAgent(session, llm=llm, settings=settings, audit=audit)
    raise ValueError(f"unknown agent {name!r}; expected one of {AGENT_NAMES}")
