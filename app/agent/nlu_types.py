"""NLU output types — re-exported from ``app.domain.nlu_types``.

Kept so existing ``from app.agent.nlu_types import …`` imports keep working.
Canonical definitions live in domain so ``sim/`` can use them without
importing ``app.agent``.
"""

from __future__ import annotations

from app.domain.nlu_types import ExtractedTerm, TurnAnalysis

__all__ = ["ExtractedTerm", "TurnAnalysis"]
