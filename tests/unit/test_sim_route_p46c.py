"""Phase 46c: the eval ``sim`` route (Groq-played rep) and the rewrite token budget."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.domain.actions import Action, Intent, Phase
from app.llm.client import parse_route_entry
from sim.creditor import SIM_MAX_TOKENS, CreditorPolicy
from sim.scenarios import generate_one

_GPT_OSS = {"groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b"}


def _profiles() -> dict[str, Any]:
    data = yaml.safe_load(Path("config/providers.yaml").read_text(encoding="utf-8"))
    return data["profiles"]


def test_eval_sim_route_order_and_low_effort() -> None:
    route = [parse_route_entry(e) for e in _profiles()["eval"]["sim"]]
    assert [t.spec for t in route] == [
        "groq/openai/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        "gemini/gemini-3.1-flash-lite",
    ]
    for t in route:
        if t.spec in _GPT_OSS:
            # Reasoning must not use up the rewrite budget (P46a: empty replies).
            assert t.params == {"reasoning_effort": "low"}
        assert not t.budgeted


def test_demo_profile_has_no_sim_route() -> None:
    """The demo profile is not touched by the sim route change."""
    assert "sim" not in _profiles()["demo"]


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def chat_text(self, role: str, messages: list[dict[str, Any]], max_tokens: int) -> str:
        self.calls.append((role, max_tokens))
        return ""


@pytest.mark.asyncio
async def test_rewrite_call_has_room_for_reasoning() -> None:
    llm = _RecordingLLM()
    rep = CreditorPolicy(generate_one("flexible", "deal", seed=101), phrasing="llm", llm=llm)
    action = Action(
        intent=Intent.OPENING,
        text_slots={},
        facts={},
        required=set(),
        next_phase=Phase.NEGOTIATE,
        reason="t",
    )
    reply = await rep.respond(action, agent_text="Hello")
    assert llm.calls == [("sim", SIM_MAX_TOKENS)]
    assert SIM_MAX_TOKENS >= 400
    # An empty reply still falls back to the draft and is counted.
    assert reply.text
    assert rep.rewrite_stats["fallbacks"] == {"empty": 1}
