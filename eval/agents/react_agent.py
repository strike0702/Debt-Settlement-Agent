"""Arm C: a ReAct tool-calling negotiator (REVIEW_PLAN §2(c) hybrid H2). EVAL-ONLY.

APPROVED RULE BREAK: the LLM chooses every move and writes the spoken line,
numbers included, and it sees client financials and the private engine ceiling
(as a real ReAct agent must, to negotiate). That breaks two CLAUDE.md ground
rules on purpose; it is what the A/B measures. Never import this from ``app/``.

Loop (per creditor turn, after the shared NLU → belief → affordability step in
``eval.agents.base``): up to ``MAX_STEPS`` (4) LLM calls. Each call returns one
JSON tool call. ``get_rules`` / ``evaluate_offer`` are observations: the result
is appended to the scratchpad and the loop continues. Any other tool is
terminal: it is mapped to an ``Action`` (``LLMArmAgent.build_action``) and its
``text`` is spoken. Terminal tools are *guarded* by the engine: an infeasible
counter/confirm, a repeated term change, or a wrap with no confirmed schedule
is rejected back to the model as an observation and costs a step. On the last
step only terminal tools are accepted; if the cap is hit anyway the agent says a
neutral line (audited ``fallback``). ``last_turn_llm_calls`` counts the calls.

Prompt design (``SYSTEM_PROMPT`` below, built to give this arm its best shot):
- ``NEGOTIATION_RULES``: goal (lowest acceptable %, never above the ceiling, a
  valid schedule beats a fast one), hard privacy rules (never say or hint at
  PRIVATE figures; refuse_private / refuse_commit), number discipline (speak only
  creditor or tool figures), and the playbook the policy encodes (learn rules,
  read back TENTATIVE, clarify CONTRADICTED, ask, counter in steps ≤4, term
  changes when unaffordable, confirm before wrap, no-deal / escalate rules);
- ``MOVE_DOCS``: every move with argument units (bp as a ``"45%"`` string);
- the ReAct protocol: think in ``thought``, check feasibility with
  ``evaluate_offer`` before quoting a schedule, one JSON object per step, and
  ``text`` must be the exact words to say (empty for observations).
The user message is the context block (finances, ceiling, belief, negotiation,
transcript) plus this turn's scratchpad and the steps left.

Routing: role ``agent`` (see ``base.AGENT_ROLE``). Called only via
``eval.agents.make_agent("react", ...)``.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.actions import Action
from eval.agents.base import (
    MOVE_DOCS,
    NEGOTIATION_RULES,
    OBSERVE_TOOLS,
    TERMINAL_TOOLS,
    LLMArmAgent,
    Move,
    MoveError,
    parse_json_object,
)

MAX_STEPS = 4

SYSTEM_PROMPT = f"""{NEGOTIATION_RULES}

You work in ReAct steps. Each step, reply with ONE JSON object and nothing else:
{{"thought": "<brief reasoning>", "tool": "<name>", "args": {{...}},
 "text": "<exact words to say>"}}

Observation tools (no speech; the result comes back to you, then you continue):
- get_rules(): your current understanding of each creditor rule and its status.
- evaluate_offer(bp): run the payment engine at bp. Returns feasible plus the
  PUBLIC schedule figures (offer_total, num_payments, first_payment_date, ...).
  Use it before you quote any schedule figure.

{MOVE_DOCS}

Every move above ends your turn: "text" is spoken to the rep exactly as written,
so it must match the move (a counter states its percentage; a confirm reads the
schedule figures from evaluate_offer). Moves are checked by the engine: an
infeasible percentage, a repeated term change, or a wrap before a confirmed
schedule is rejected and you must choose again. You have at most {MAX_STEPS} steps
per turn; on the last step you must choose a move."""


class _StepCall(BaseModel):
    """One ReAct step as returned by the model."""

    model_config = ConfigDict(extra="ignore")

    thought: str = ""
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    text: str = ""


class ReactAgent(LLMArmAgent):
    """Arm C: LLM picks tools in a capped loop; engine-guarded terminal moves."""

    name = "react"
    guarded = True

    def _user_prompt(self, scratch: list[str], steps_left: int) -> str:
        parts = [self.context_block(), "", "## This turn"]
        parts += scratch or ["(no steps yet)"]
        if steps_left == 1:
            parts.append("LAST STEP: you must choose a move now (no observation tools).")
        else:
            parts.append(f"Steps left this turn: {steps_left}.")
        return "\n".join(parts)

    async def _observe(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "get_rules":
            return self.tool_get_rules()
        return await self.tool_evaluate_offer(args)

    async def decide(self) -> tuple[Action, str]:
        """Run the capped tool loop; return the terminal move's Action and line."""
        scratch: list[str] = []
        for step in range(MAX_STEPS):
            n = step + 1
            steps_left = MAX_STEPS - step
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._user_prompt(scratch, steps_left)},
            ]
            raw = await self.call_llm(messages)
            try:
                call = _StepCall.model_validate(parse_json_object(raw))
            except (ValueError, ValidationError) as e:
                scratch.append(f"step {n}: invalid reply ({str(e)[:160]}); send one JSON object")
                self._audit("step_invalid", {"step": n, "raw": (raw or "")[:300]})
                continue
            args_s = json.dumps(call.args, default=str)
            self._audit("step", {"step": n, "tool": call.tool, "args": call.args})
            if call.tool in OBSERVE_TOOLS:
                if steps_left == 1:
                    scratch.append(f"step {n}: {call.tool} refused: last step needs a move")
                    continue
                try:
                    result: Any = await self._observe(call.tool, call.args)
                except MoveError as e:
                    result = {"error": str(e)}
                scratch.append(
                    f"step {n}: {call.tool}({args_s}) -> {json.dumps(result, default=str)}"
                )
                continue
            if call.tool not in TERMINAL_TOOLS:
                scratch.append(f"step {n}: unknown tool {call.tool!r}")
                continue
            text = call.args.get("text", "") if call.tool == "say" else call.text
            try:
                action = await self.build_action(Move(call.tool, call.args, str(text or "")))
            except MoveError as e:
                scratch.append(f"step {n}: {call.tool}({args_s}) REJECTED: {e}")
                self._audit("step_rejected", {"step": n, "tool": call.tool, "error": str(e)})
                continue
            return action, str(text or call.text or "")
        return self.fallback_action("step_cap")
