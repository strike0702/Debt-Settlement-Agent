"""Scripted calls: fixed rep lines run end to end through the orchestrator.

Each YAML script under ``eval/scripted_calls/scripts/`` names a scenario and a
list of rep turns; each turn may carry an ``oracle`` analysis (for
``--nlu oracle``) and an ``expect`` block: the agent move that must follow
(``intent``, optional ``reason``, optional ``facts`` as ``{id: value}`` with
percent facts in bp and money in cents). The runner (``run_script``) drives a
real ``Orchestrator`` (auto-ack, template NLG) with the chosen NLU:

- ``oracle``: the turn's ``oracle`` acts (a ``RepTurn``, default ``{}`` = no
  act) through ``verify_acts`` and the code cues, no model;
- ``acts``: the live ``nlu`` role (the flat schema was removed in 71b; its
  frozen records stay under ``docs/eval``).

A script with ``requires: phase69`` is skipped (with a note) until Phase 69's
operator rules are on main. A script-level ``known_rules`` map seeds
operator-entered rules (Phase 69 panel) and ``expect.known_rules`` (field →
``rep`` | ``operator``) checks each rule's source in the call context the NLU
saw that turn. Eval only; ``app/`` never imports it. CLI:
``python -m eval.scripted_calls.run --nlu acts --providers PATH --profile NAME``.
"""
