"""Agents under test for the three-arm A/B (REVIEW_PLAN §2(c), Phase 24a).

``protocol`` defines ``AgentUnderTest`` and the ``PolicyAgent`` adapter over the
production ``Orchestrator`` (arm A). ``react_agent`` (arm C) and
``llm_only_agent`` (arm D) let an LLM choose moves and write numbers: an
explicit, approved, eval-only break of two CLAUDE.md ground rules, because that
is what the experiment measures. Nothing under ``app/`` may import this package.
``eval.run_eval --agent`` picks the arm via ``make_agent``.
"""

from __future__ import annotations

from eval.agents.protocol import AGENT_NAMES, AgentName, AgentUnderTest, PolicyAgent, make_agent

__all__ = ["AGENT_NAMES", "AgentName", "AgentUnderTest", "PolicyAgent", "make_agent"]
