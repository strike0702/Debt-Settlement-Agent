"""Creditor simulator package (scenarios, personas, code-based policy).

``sim/`` must not import ``app.agent`` — shared Action / TurnAnalysis types
live in ``app.domain``. Eval and e2e drive calls through ``CreditorPolicy``.
"""

from __future__ import annotations
