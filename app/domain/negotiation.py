"""Accept line shared by the agent policy and the simulator's scenario labels.

Phase 45 (user decision 2026-10-09): the agent accepts a settlement only at or
below the *accept line*, 75% of the client's ceiling (``Affordability.max_bp``),
and never counters above it. The policy reads the percentage from
``Settings.accept_line_pct_of_max_bp``; ``sim.scenarios`` uses the default here
to label which scenarios have a deal (``zopa``), so ``sim/`` never imports
``app.agent``. Integer math only: bp in, bp out, rounded down.
"""

from __future__ import annotations

# 7500 = 75.00% of max_bp, in basis points of max_bp.
ACCEPT_LINE_PCT_OF_MAX_BP = 7500


def accept_line_bp(max_bp: int, pct_of_max_bp: int = ACCEPT_LINE_PCT_OF_MAX_BP) -> int:
    """Highest settlement bp we may accept: ``max_bp * pct_of_max_bp / 10000``, rounded down."""
    return max_bp * pct_of_max_bp // 10000
