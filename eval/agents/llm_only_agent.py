"""Arm D: a pure LLM-only negotiator, one call per turn, no tools. EVAL-ONLY.

APPROVED RULE BREAK: the LLM chooses every move and writes the spoken line,
numbers included, from a prompt that carries client financials and the private
engine ceiling. That breaks two CLAUDE.md ground rules on purpose; it is the
baseline the A/B measures against. Never import this from ``app/``.

Per creditor turn, after the shared NLU → belief → affordability step in
``eval.agents.base``: exactly one LLM call returning
``{"text": "<words to say>", "move": {"tool": "<move>", "args": {...}}}``.
The move is mapped to an ``Action`` *unguarded*: an infeasible counter or
confirm, or a wrap with no confirmed schedule, goes through as chosen (engine
figures are attached when the engine can produce them, so the sim can judge
it). A reply that does not parse or names an unknown move becomes a neutral
``say`` (audited ``fallback``); there is no retry, so calls per turn stay 1.

Prompt design: the same ``NEGOTIATION_RULES`` and ``MOVE_DOCS`` as the ReAct
arm (goal, privacy, numbers, playbook, move units) plus the single-reply JSON
contract; the context block shows the ceiling and the feasible percentages, so
the model has what ``evaluate_offer`` would tell it about price, but not the
schedule figures. Routing: role ``agent`` (see ``base.AGENT_ROLE``). Called only
via ``eval.agents.make_agent("llm_only", ...)``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.actions import Action
from eval.agents.base import (
    MOVE_DOCS,
    NEGOTIATION_RULES,
    TERMINAL_TOOLS,
    LLMArmAgent,
    Move,
    MoveError,
    parse_json_object,
)

SYSTEM_PROMPT = f"""{NEGOTIATION_RULES}

{MOVE_DOCS}

Reply with ONE JSON object and nothing else:
{{"text": "<exact words to say to the rep>", "move": {{"tool": "<move>", "args": {{...}}}}}}
"text" is spoken exactly as written and must match the move (a counter states
its percentage; a confirm states the settlement percentage and the schedule)."""


class _MoveOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = ""
    move: _MoveOut


class LLMOnlyAgent(LLMArmAgent):
    """Arm D: one LLM call picks the move and writes the line; no guards."""

    name = "llm_only"
    guarded = False

    async def decide(self) -> tuple[Action, str]:
        """One call; parse failure or a bad move falls back to a neutral line."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self.context_block()},
        ]
        raw = await self.call_llm(messages)
        try:
            reply = _Reply.model_validate(parse_json_object(raw))
        except (ValueError, ValidationError):
            self._audit("reply_invalid", {"raw": (raw or "")[:300]})
            return self.fallback_action("invalid_reply")
        tool = reply.move.tool
        if tool not in TERMINAL_TOOLS:
            self._audit("move_invalid", {"tool": tool})
            return self.fallback_action("unknown_move")
        try:
            action = await self.build_action(Move(tool, reply.move.args, reply.text))
        except MoveError as e:
            self._audit("move_invalid", {"tool": tool, "error": str(e)})
            return self.fallback_action("bad_move_args")
        return action, reply.text
