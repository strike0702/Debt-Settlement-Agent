"""Apply an ``Action``'s deferred effects to the call's negotiation state.

The policy never mutates state: each move carries ``Action.effects`` (record
the rep's ask, offer a counter, record a confirm, move the phase, note a term
alternative, ...). ``apply_effects`` commits them to ``CallSession.neg``. The
orchestrator calls it once the move has been heard (speech ack), and eagerly
on emit for its bookkeeping kinds so a fast accept sees the offer; a
barge-in drops the other effects of words the rep never heard.
``offer_counter`` and ``note_terms_countered`` tolerate a second apply.
"""

from __future__ import annotations

from app.agent.policy import (
    Effect,
    Phase,
    terms_counter_key,
)
from app.agent.session import CallSession


def apply_effects(session: CallSession, effects: list[Effect]) -> None:
    """Commit deferred negotiation side effects (speech ack path only)."""
    for effect in effects:
        kind = effect.kind
        data = effect.data
        if kind == "offer_counter":
            bp = int(data["bp"])
            # Idempotent: eager emit + barge-in/ack must not double-count.
            if not session.neg.counters_offered or session.neg.counters_offered[-1] != bp:
                session.neg.counters_offered.append(bp)
            # Price-ladder bookkeeping: plain sets, so a second apply is a no-op.
            if "ask_bp" in data:
                session.neg.ask_at_last_counter = int(data["ask_bp"])
            if "stage" in data:
                session.neg.hold_stage = int(data["stage"])
            if "final_ask" in data:
                session.neg.final_counter_ask = int(data["final_ask"])
            turn = data.get("turn")
            if turn is not None and int(turn) not in session.neg.counter_turns:
                session.neg.counter_turns.append(int(turn))
        elif kind == "note_question":
            key = str(data["key"])
            session.neg.question_counts[key] = session.neg.question_counts.get(key, 0) + 1
        elif kind == "set_pending_readback":
            session.neg.pending_readback = str(data["field"])
        elif kind == "clear_pending_readback":
            session.neg.pending_readback = None
        elif kind == "inc_private_ask":
            session.neg.private_ask_count += 1
        elif kind == "inc_commit_demand":
            session.neg.commit_demand_count += 1
        elif kind == "record_ask":
            bp = int(data["bp"])
            session.neg.ask_bp = bp
            session.neg.ask_history.append(bp)
        elif kind == "record_confirm":
            raw_key = data.get("key")
            if isinstance(raw_key, list):
                session.neg.last_confirm_key = tuple(raw_key)
            elif isinstance(raw_key, tuple):
                session.neg.last_confirm_key = raw_key
            else:
                session.neg.last_confirm_key = (int(data["ask_bp"]),)
            session.neg.confirmed_bp = int(data["ask_bp"])
        elif kind == "note_confirm_accepted":
            session.neg.accepted_confirm_key = tuple(data["key"])
        elif kind == "inc_confirm_reject":
            session.neg.confirm_rejects += 1
        elif kind == "note_assumed_asked":
            session.neg.assumed_asked.add(str(data["field"]))
        elif kind == "note_clarify":
            fname = str(data["field"])
            session.neg.clarify_counts[fname] = session.neg.clarify_counts.get(fname, 0) + 1
        elif kind == "note_terms_countered":
            key = str(data.get("key") or terms_counter_key(str(data["field"]), data["value"]))
            if key not in session.neg.terms_countered:
                session.neg.terms_countered.append(key)
            session.neg.pending_terms_alt = {
                "field": str(data["field"]),
                "value": data["value"],
            }
        elif kind == "clear_pending_terms_alt":
            session.neg.pending_terms_alt = None
        elif kind == "clear_wrap":
            session.agreement = None
            session.agreed_bp = None
            session.last_eval = None
        elif kind == "set_pending_cents_clarify":
            session.neg.pending_cents_clarify = dict(data)
        elif kind == "clear_pending_cents_clarify":
            session.neg.pending_cents_clarify = None
        elif kind == "set_phase":
            session.neg.phase = Phase(str(data["phase"]))
