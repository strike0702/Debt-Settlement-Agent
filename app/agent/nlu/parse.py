"""Parse the NLU model's JSON reply into a ``TurnAnalysis``.

``coerce_analysis_payload`` repairs common shape mistakes (per-field blobs
instead of ``terms``, ``{value, quote}`` objects for the ask, an off-list
question topic) before Pydantic validation; ``coerce_tiers`` turns LLM or
oracle tier lists into the engine's sorted ``[(from_payment, min_cents)]``
or rejects them whole. Shape only: no quote checks or value verification,
which happen afterwards in ``verify.post_verify``.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.nlu_types import TurnAnalysis
from app.domain.fields import FIELDS_BY_NAME
from app.domain.nlu_types import QUESTION_TOPICS
from app.llm.client import strip_json_fences


def coerce_analysis_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize common LLM shape mistakes into ``TurnAnalysis`` fields."""
    out = dict(data)
    terms_raw = out.get("terms")
    terms: list[Any] = list(terms_raw) if isinstance(terms_raw, list) else []

    if not terms:
        for name in FIELDS_BY_NAME:
            blob = out.pop(name, None)
            if isinstance(blob, dict) and "value" in blob:
                terms.append(
                    {
                        "field": name,
                        "value": blob.get("value"),
                        "quote": blob.get("quote", ""),
                        "hedged": bool(blob.get("hedged", False)),
                    }
                )

    ask = out.get("settlement_ask_pct")
    if isinstance(ask, dict):
        out["settlement_ask_pct"] = ask.get("value")
        if not out.get("ask_quote"):
            out["ask_quote"] = ask.get("quote")

    total = out.get("settlement_ask_total_cents")
    if isinstance(total, dict):
        out["settlement_ask_total_cents"] = total.get("value")
        if not out.get("ask_total_quote"):
            out["ask_total_quote"] = total.get("quote")
    amb = out.get("amount_ambiguous")
    if isinstance(amb, dict) and out.get("amount_ambiguous_cents") is None:
        out["amount_ambiguous_cents"] = amb.get("cents", amb.get("value"))
        out["amount_ambiguous_quote"] = amb.get("quote")
    out.pop("amount_ambiguous", None)

    out["terms"] = terms
    # An off-list topic must not fail validation of the whole analysis.
    topic = out.get("question_topic")
    if topic is not None and topic not in QUESTION_TOPICS:
        out["question_topic"] = "other"
    return out


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


def _parse_analysis(text: str) -> TurnAnalysis:
    raw = strip_json_fences(text)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("NLU JSON root must be an object")
    return TurnAnalysis.model_validate(coerce_analysis_payload(data))
