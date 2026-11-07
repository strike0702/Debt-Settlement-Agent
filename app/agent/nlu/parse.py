"""Shape coercion for rule values the NLU model or the oracle supplies.

``coerce_tiers`` turns LLM or oracle tier lists into the engine's sorted
``[(from_payment, min_cents)]`` or rejects them whole. Shape only: no quote
checks or value verification (``verify.post_verify`` does those). The model's
act list itself is parsed by ``acts.parse_acts``.
"""

from __future__ import annotations

from typing import Any


def coerce_tiers(raw: Any) -> list[tuple[int, int]] | None:
    """LLM / oracle tiers → engine ``[(from_payment, min_cents)]`` sorted; None if malformed.

    Items are ``{"from_payment", "min_cents"}`` dicts (LLM) or 2-int pairs
    (oracle). Any bad item, a non-list, or a repeated ``from_payment``
    rejects the whole value rather than guessing.
    """
    if not isinstance(raw, list):
        return None
    out: list[tuple[int, int]] = []
    for item in raw:
        if isinstance(item, dict):
            if set(item) != {"from_payment", "min_cents"}:
                return None
            pair = (item["from_payment"], item["min_cents"])
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            pair = (item[0], item[1])
        else:
            return None
        frm, cents = pair
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in pair):
            return None
        if frm < 1 or cents <= 0:
            return None
        out.append((frm, cents))
    if len({frm for frm, _ in out}) != len(out):
        return None
    return sorted(out)
