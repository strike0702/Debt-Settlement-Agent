"""Sim creditor schedule validation (REVIEW F15)."""

from __future__ import annotations

import pytest

from app.domain.actions import Action, Intent, Phase
from sim.creditor import CreditorPolicy
from sim.scenarios import generate_one


@pytest.mark.asyncio
async def test_speak_schedule_validates_like_confirm() -> None:
    """F15: SPEAK_SCHEDULE must not auto-accept without schedule checks."""
    sc = generate_one("flexible", "deal", seed=101)
    creditor = CreditorPolicy(sc, phrasing="template", llm=None)
    action = Action(
        intent=Intent.SPEAK_SCHEDULE,
        text_slots={},
        facts={},
        required=set(),
        next_phase=Phase.NEGOTIATE,
        reason="test",
    )
    reply = await creditor.respond(action, agent_text="here is a schedule")
    # Missing settlement facts → reject (same path as CONFIRM_SCHEDULE).
    assert reply.analysis.stance == "reject"
