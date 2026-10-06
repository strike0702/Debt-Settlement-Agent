"""Role-scoped WS streams (REVIEW_PLAN F7): what ``?view=rep`` may see.

``redact_for_view`` is the single choke point: ``app.voice.ws`` passes every
outgoing frame through it. The operator stream gets frames unchanged except
that audit rows carry ``private: true`` when the rep may not see them. The rep
("creditor's eye") stream drops the affordability curve and ``max_bp``, firm
fees, the client's savings balances, rescue amounts, guard ``offending`` tokens
and private audit rows. Returns ``None`` when a frame must not be sent at all.

Policy, NLG and the audit log are unchanged; this only filters what leaves the
socket. Tested by running whole calls on the rep view and scanning every frame
for any value in ``session.private_blocklist``.
"""

from __future__ import annotations

from typing import Any

from app.schemas.events import View

# Audit actors whose rows carry engine results (max_bp, rescue amounts), the
# drafted agreement with fees and balances, or LLM provider metadata.
_PRIVATE_AUDIT_ACTORS = frozenset({"engine", "agent", "llm"})
# Events whose payloads can hold private figures: guard hits name the blocked
# number; committed / dropped effects carry the confirm fingerprint, which
# includes the firm's fee rules.
_PRIVATE_AUDIT_EVENTS = frozenset(
    {"blocked", "effects_committed", "barge_in", "nlg_template_rejected", "llm_unavailable"}
)
_REP_ROW_KEYS = ("date", "creditor_payment_cents")


def is_private_audit(actor: str, event: str) -> bool:
    """True when an audit row must stay off the rep stream."""
    return actor in _PRIVATE_AUDIT_ACTORS or event in _PRIVATE_AUDIT_EVENTS


def _rep_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if rows is None:
        return None
    return [{k: r[k] for k in _REP_ROW_KEYS if k in r} for r in rows]


def redact_for_view(payload: dict[str, Any], view: View) -> dict[str, Any] | None:
    """Return the frame as ``view`` may see it, or ``None`` to drop it."""
    kind = payload.get("type")
    if kind == "audit":
        private = is_private_audit(str(payload.get("actor")), str(payload.get("event")))
        if view == "rep" and private:
            return None
        return {**payload, "private": private}
    if view == "operator":
        return payload

    out = dict(payload)
    if kind == "eval":
        for key in ("max_bp", "program_fee_cents", "additional_funds"):
            out.pop(key, None)
        out["rows"] = _rep_rows(out.get("rows"))
    elif kind == "agreement":
        out["rows"] = _rep_rows(out.get("rows")) or []
    elif kind == "blocked":
        out.pop("offending", None)
    elif kind == "turn_trace":
        out.pop("affordability", None)
        nlg = dict(out["nlg"])
        nlg["guards"] = [{**g, "offending": None} for g in nlg.get("guards", [])]
        out["nlg"] = nlg
    return out
