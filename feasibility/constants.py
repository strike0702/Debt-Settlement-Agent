"""Engine constants: rounding notes, soft-buffer and cliff knobs, Part 2 guardrails.

Pure data, imported by ``engine.py``, ``scoring.py`` and ``rescue.py``. The Tier C
buffer defaults to 0 (strict take-home compliance); Tier B cliff flattening is on.
Guardrail percentages are Decimals and are applied with ``util.round_half_up``.
Changing any value here changes engine output: it needs a golden-corpus update and a
PROGRESS note (see CLAUDE.md).
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

# --- Rounding / calendar conventions -------------------------------------
# All spec round(...) calls are half-up (away from zero). Implemented via
# Decimal quantize with ROUND_HALF_UP; builtin round() is forbidden for money.
# Cadence: EOM-preserving when first_payment_date is the last day of its month,
# otherwise day-of-month clamped to month length (models.monthly_payment_dates).

# --- Tier C: soft client cushion (customer protection, two-pass) ----------
# Since Phase 59 the buffer is per firm and per call: ``Problem.min_cushion_cents``
# (from ``firm.json`` ``min_cushion_cents``, overridable at call start). The engine
# tries it first and falls back to 0, so it never changes feasibility. The take-home
# wrapper ``evaluate_offer`` builds problems with 0 (strict assignment compliance).

# --- Tier B: cliff flattening (post-pass, firm-indifferent) ----------------
ENABLE_CLIFF_FLATTENING: bool = True
MAX_CLIFF_RATIO: Fraction = Fraction(3, 1)  # target: p_k <= 3 x lower-median payment

# --- Part 2 guardrails (from ASSIGNMENT.md §8; centralized, not tunable) ---
GUARDRAIL_LUMP_SUM_PCT: Decimal = Decimal("0.65")  # L <= round_half_up(0.65 * offer_total)
GUARDRAIL_INCREMENT_PCT: Decimal = Decimal("0.40")  # X <= max(floor, round_half_up(0.40 * draft))
GUARDRAIL_INCREMENT_FLOOR_CENTS: int = 10000

# --- Determinism ------------------------------------------------------------
SHAPE_TIEBREAK_ORDER: dict[str, int] = {"even": 0, "staircase": 1, "balloon": 2}
