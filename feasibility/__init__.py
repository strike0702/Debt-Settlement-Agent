"""Settlement feasibility engine (owned v2; started as the take-home, vendored in Phase 0).

Given a client's SDA ledger, a creditor offer and the creditor's rules, decide whether
a legal payment schedule exists, return the fee-first schedule if so, and the minimal
extra funding (lump sum, monthly increment) if not. Entry points: ``engine.evaluate``
(with a ``problem.Problem`` built once per client) and ``engine.evaluate_offer``
(take-home API). Money is integer cents, settlement and fee rates integer basis points.
Callers: ``app.adapter.engine_adapter`` (the agent), ``sim/`` and ``eval/``.
"""
