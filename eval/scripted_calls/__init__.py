"""Scripted calls: fixed rep lines run end to end through the orchestrator.

Each YAML script under ``eval/scripted_calls/scripts/`` names a scenario and a
list of rep turns; each turn may carry an ``oracle`` analysis (for
``--nlu oracle``) and an ``expect`` block: the agent move that must follow
(``intent``, optional ``reason``, optional ``facts`` as ``{id: value}`` with
percent facts in bp and money in cents). The runner (``run_script``) drives a
real ``Orchestrator`` (auto-ack, template NLG) with the chosen NLU:

- ``oracle``: the turn's ``oracle`` TurnAnalysis (default ``{"stance": "info"}``)
  through ``post_verify`` and the code cues, no model;
- ``flat`` / ``acts``: the live ``nlu`` role on that schema (Phase 71a A/B).

A script with ``requires: phase69`` is skipped (with a note) until Phase 69's
operator rules are on main. Eval only; ``app/`` never imports it. CLI:
``python -m eval.scripted_calls.run --nlu acts --providers PATH --profile NAME``.
"""
