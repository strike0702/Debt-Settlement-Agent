"""Role-scoped WS streams (REVIEW_PLAN F7): what ``?view=rep`` may see.

``redact_for_view`` is the single choke point: ``app.voice.ws`` passes every
outgoing frame through it. The operator ("Debt negotiator") stream gets frames
unchanged except that audit rows carry ``private: true`` when the rep may not
see them. The rep ("Creditor rep") stream gets no ``turn_trace`` at all (the
decision trace is the negotiator's tool, Phase 35), and drops ``max_bp``, firm
fees, the client's savings balances, rescue amounts, ``blocked.offending`` and
private audit rows. Phase 48: it also drops the policy's reason code (the
``escalate.reason`` of a handoff and the ``reason`` of ``policy/decide`` rows),
since codes such as ``above_accept_line`` or ``out_of_guardrail`` tell the rep
why the agent stopped; the rep keeps the spoken ``escalate_reason``. Returns
``None`` when a frame must not be sent at all.

Policy, NLG and the audit log are unchanged; this only filters what leaves the
socket. Tested by running whole calls on the rep view and scanning every frame
for any value in ``session.private_blocklist``.
"""

from __future__ import annotations

from typing import Any

from app.schemas.events import View

# Allow-list of audit rows the rep may see, as (actor, event) pairs. Anything
# not listed stays operator-only, so a new audit event is private until someone
# checks its payload and adds it here. Deliberately absent: every ``engine`` row
# (max_bp, rescue amounts), ``agent/agreement_drafted`` (fees, balances), every
# ``llm`` row (provider metadata), ``nlg/blocked`` and ``nlg_template_rejected``
# (name the blocked number), ``*/llm_unavailable``, ``orchestrator/barge_in``
# and ``effects_committed`` (the confirm fingerprint includes fee rules).
_REP_AUDIT_EVENTS: dict[str, frozenset[str]] = {
    "belief": frozenset({"accept_terms_alt", "confirm_readback", "observe", "revise"}),
    "client": frozenset({"timing"}),
    "creditor": frozenset({"utterance", "utterance_source"}),
    "nlg": frozenset({"act_dropped"}),
    "nlu": frozenset(
        {
            "analysis",
            "cents_clarify_out_of_range",
            "cents_clarify_resolved",
            "fast_field_answer",
            "fast_readback",
            "nlu_cents_ambiguity",
            "nlu_rejected_ask_value",
            "nlu_rejected_bare_year",
            "nlu_rejected_date",
            "nlu_rejected_quote",
            "nlu_rejected_range",
            "nlu_rejected_readback",
            "nlu_rejected_tiers",
            "nlu_repaired_cents",
            "nlu_tiers_ambiguous",
            "nlu_validation_failed",
        }
    ),
    "orchestrator": frozenset(
        {
            "autoplay_done",
            "call_ended",
            "call_started",
            "nlu_cancel_merge",
            "nlu_restart",
            "post_nlu_drain",
            "sentence_done",
            "start",
            "text_queued",
            "turn_complete",
            "validator",
            "wrap_failed_no_deal",
            "wrap_infeasible",
            "wrap_missing_eval",
            "wrap_needs_info",
        }
    ),
    "policy": frozenset({"decide"}),
    "stt": frozenset({"stt_error"}),
}
_REP_ROW_KEYS = ("date", "creditor_payment_cents")


def is_private_audit(actor: str, event: str) -> bool:
    """True when an audit row must stay off the rep stream (anything not allow-listed)."""
    return event not in _REP_AUDIT_EVENTS.get(actor, frozenset())


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
        out = {**payload, "private": private}
        body = out.get("payload")
        if view == "rep" and payload.get("actor") == "policy" and isinstance(body, dict):
            out["payload"] = {k: v for k, v in body.items() if k != "reason"}
        return out
    if view == "operator":
        return payload

    if kind == "turn_trace":
        return None
    out = dict(payload)
    if kind == "eval":
        for key in ("max_bp", "program_fee_cents", "additional_funds"):
            out.pop(key, None)
        out["rows"] = _rep_rows(out.get("rows"))
    elif kind == "agreement":
        out["rows"] = _rep_rows(out.get("rows")) or []
    elif kind == "blocked":
        out.pop("offending", None)
    elif kind == "escalate":
        out["reason"] = None
    return out
