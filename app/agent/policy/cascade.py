"""``decide``: the first-match cascade that picks one move per rep turn.

The step order is load-bearing (see the package docstring): interruptions,
clarify / read-back, confirm, discovery, negotiate. Each ``_decide_*`` returns
an ``Action`` or ``None`` to fall through. After the cascade, ``loop_guard``
hands off instead of asking the same question a third time, and
``rep_turn_progress`` tells the orchestrator whether a rep turn added anything
(the no-progress count). Pure: no I/O, no LLM.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.adapter.engine_adapter import Affordability
from app.agent.policy.ladder import _confirm_required, _negotiate
from app.agent.policy.moves import (
    _ask_field,
    _confirm_action,
    _confirm_key,
    _counter_terms_action,
    _escalate,
    _first_assumed_field,
    _handoff,
    _spoken_value,
    follow_up_action,
)
from app.agent.policy.state import (
    NegotiationState,
    _Turn,
    ask_pct_to_bp,
    field_already_countered,
    terms_counter_key,
)
from app.config import Settings, get_settings
from app.domain.actions import Action, Effect, Intent, Phase
from app.domain.belief import BeliefChange, BeliefState, TermStatus
from app.domain.facts import Fact
from app.domain.fields import FIELDS_BY_NAME
from app.domain.negotiation import accept_line_bp
from app.domain.nlu_types import TurnAnalysis

# Moves that ask the rep something; the loop guard counts them per key.
_QUESTION_INTENTS = frozenset(
    {Intent.ASK, Intent.ASK_SETTLEMENT, Intent.CLARIFY, Intent.READ_BACK}
)


def question_key(action: Action) -> str | None:
    """``INTENT:reason`` for a question move (same intent + field / template), else None."""
    if action.intent not in _QUESTION_INTENTS:
        return None
    return f"{action.intent.value}:{action.reason or ''}"


def loop_guard(action: Action, neg: NegotiationState, cfg: Settings) -> Action:
    """Hand off instead of asking the same question a third time; else count this ask.

    The count is an ``note_question`` effect, applied when the move is spoken.
    """
    key = question_key(action)
    # ``_decide_wrap`` re-enters ``decide``; count each spoken question once.
    if key is None or any(e.kind == "note_question" for e in action.effects):
        return action
    if neg.question_counts.get(key, 0) >= cfg.max_same_question:
        kept = [e for e in action.effects if e.kind in ("record_ask", "clear_pending_terms_alt")]
        return _handoff(kept, "repeated_question")
    return action.model_copy(
        update={"effects": [*action.effects, Effect(kind="note_question", data={"key": key})]}
    )


def rep_turn_progress(
    analysis: TurnAnalysis,
    neg: NegotiationState,
    belief_changes: list[BeliefChange],
    *,
    accepted_term_alt: bool = False,
) -> bool:
    """True when this rep turn added verified information or moved the call.

    Progress: a belief value or status changed, a new ask, an accept or reject,
    a read-back answer, a firm stance, or a yes to a non-price alternative.
    A restated ask, a repeated term, a question or small talk is not progress.
    """
    if any(
        c.old_value != c.new_value or c.old_status != c.new_status for c in belief_changes
    ):
        return True
    if (
        analysis.settlement_ask_pct is not None
        and ask_pct_to_bp(analysis.settlement_ask_pct) != neg.ask_bp
    ):
        return True
    return (
        analysis.stance in ("accept", "reject")
        or analysis.readback_response in ("confirm", "deny")
        or analysis.firm
        or accepted_term_alt
    )


def _close_after_wrap(effects: list[Effect], *, reason: str) -> Action:
    """Polite thanks after a proposal was already sent to the client."""
    return Action(
        intent=Intent.CLOSE,
        effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
        next_phase=Phase.END,
        reason=reason,
    )


def _wrap_should_renegotiate(analysis: TurnAnalysis) -> bool:
    """True when the rep reopens price or terms after PROPOSE_WRAP."""
    if analysis.settlement_ask_pct is not None:
        return True
    if analysis.stance in ("reject", "counter", "offer"):
        return True
    if analysis.terms:
        return True
    return False


def decide(
    belief: BeliefState,
    neg: NegotiationState,
    analysis: TurnAnalysis,
    afford: Affordability | None,
    *,
    settings: Settings | None = None,
    rescue_within_guardrail: bool = False,
    confirm_facts: dict[str, Fact] | None = None,
    counter_offer_total_cents: int | None = None,
    term_alt: tuple[str, Any] | None = None,
    accepted_term_alt: bool = False,
) -> Action:
    """Return the next agent ``Action``: first match of the module-docstring cascade.

    ``term_alt`` is ``(field, value)`` for the next non-price recovery stage
    when the curve is empty or the ask sits above the ceiling
    (first_payment_date → min_payment → max_payments).

    ``accepted_term_alt`` is true when this turn's accept applied a pending
    non-price alternative. That yes is not acceptance of the last price.
    """
    ask_bp = neg.ask_bp
    effects: list[Effect] = []
    if analysis.settlement_ask_pct is not None:
        ask_bp = ask_pct_to_bp(analysis.settlement_ask_pct)
        effects.append(Effect(kind="record_ask", data={"bp": ask_bp}))
    # A rejected pending non-price alt is cleared so the cascade offers the next stage.
    if (
        neg.pending_terms_alt is not None
        and analysis.stance == "reject"
        and analysis.settlement_ask_pct is None
    ):
        effects.append(Effect(kind="clear_pending_terms_alt", data={}))
        neg = replace(neg, pending_terms_alt=None)

    t = _Turn(
        belief=belief,
        neg=neg,
        analysis=analysis,
        afford=afford,
        cfg=settings or get_settings(),
        ask_bp=ask_bp,
        effects=effects,
        rescue_within_guardrail=rescue_within_guardrail,
        confirm_facts=confirm_facts,
        counter_offer_total_cents=counter_offer_total_cents,
        term_alt=term_alt,
        accepted_term_alt=accepted_term_alt,
    )
    action: Action | None = None
    for step in (_decide_interruptions, _decide_clarify, _decide_confirm, _decide_discovery):
        action = step(t)
        if action is not None:
            break
    if action is None:
        action = _decide_negotiate(t)
    return loop_guard(action, neg, t.cfg)


def _decide_interruptions(t: _Turn) -> Action | None:
    """Moves that preempt the phase flow, in priority order."""
    a, neg, cfg = t.analysis, t.neg, t.cfg
    if neg.turn_idx > cfg.max_turns:
        return _handoff(t.effects, "max_turns")
    if a.hostility >= cfg.hostility_threshold:
        return _escalate(
            t.effects,
            reason="hostile",
            spoken_reason="The tone on this call needs a specialist.",
        )
    # Private-info and commitment demands: refuse once, escalate on repeat.
    stay = neg.phase if neg.phase != Phase.OPENING else Phase.DISCOVERY
    if a.asks_client_private_info:
        bumped = t.effects + [Effect(kind="inc_private_ask")]
        if neg.private_ask_count >= 1:
            return _escalate(
                bumped,
                reason="sensitive_request",
                spoken_reason="I need to hand this off after a sensitive request.",
            )
        return Action(
            intent=Intent.REFUSE_PRIVATE,
            effects=bumped,
            next_phase=stay,
            reason="private_info",
        )
    if a.demands_commitment:
        bumped = t.effects + [Effect(kind="inc_commit_demand")]
        if neg.commit_demand_count >= 1:
            return _escalate(
                bumped,
                reason="commitment_demand",
                spoken_reason="I need to involve someone about a commitment demand.",
            )
        return Action(
            intent=Intent.REFUSE_COMMIT,
            effects=bumped,
            next_phase=stay,
            reason="commitment",
        )
    if neg.phase in (Phase.WRAP, Phase.END, Phase.ESCALATE):
        return _decide_wrap(t)
    if neg.no_progress_turns >= cfg.max_no_progress_turns:
        return _handoff(t.effects, "no_progress")
    # Schedule detail after a proposal; "payment schedule" often appears in accept lines.
    if a.asks_for_schedule and neg.last_confirm_key is not None and a.stance != "accept":
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=t.effects,
            next_phase=Phase.CONFIRM,
            reason="schedule_detail",
        )
    if a.wants_to_end and a.stance != "accept":
        # Soft-accept a schedule already on the table, then close with thanks.
        if neg.phase == Phase.CONFIRM and neg.last_confirm_key is not None:
            return Action(
                intent=Intent.CLOSE,
                facts=dict(t.confirm_facts or {}),
                effects=t.effects
                + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
                next_phase=Phase.END,
                reason="thanks_accept",
            )
        return follow_up_action(t.effects)
    return None


def _decide_wrap(t: _Turn) -> Action:
    """Post-proposal phases: stay terminal, or reopen price/terms from WRAP."""
    a, neg = t.analysis, t.neg
    if neg.phase == Phase.ESCALATE:
        return Action(
            intent=Intent.ESCALATE,
            text_slots={"escalate_reason": "A specialist still needs to join this call."},
            effects=t.effects,
            next_phase=Phase.ESCALATE,
            reason="already_escalated",
        )
    if neg.phase == Phase.END:
        return _close_after_wrap(t.effects, reason="already_ended")
    if a.asks_for_schedule and a.stance != "accept":
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=t.effects,
            next_phase=Phase.WRAP,
            reason="schedule_detail_post_wrap",
        )
    if not _wrap_should_renegotiate(a):
        return _close_after_wrap(t.effects, reason="post_wrap")
    # Re-enter as CONFIRM/NEGOTIATE; the drafted agreement clears on ack.
    reopen = Phase.CONFIRM if neg.confirmed_bp is not None else Phase.NEGOTIATE
    action = decide(
        t.belief,
        replace(neg, phase=reopen),
        a,
        t.afford,
        settings=t.cfg,
        rescue_within_guardrail=t.rescue_within_guardrail,
        confirm_facts=t.confirm_facts,
        counter_offer_total_cents=t.counter_offer_total_cents,
        term_alt=t.term_alt,
        accepted_term_alt=t.accepted_term_alt,
    )
    return action.model_copy(
        update={
            "effects": [Effect(kind="clear_wrap", data={}), *action.effects],
            "reason": action.reason or "wrap_renegotiate",
        }
    )


def _decide_clarify(t: _Turn) -> Action | None:
    """Ambiguous tiers, then CONTRADICTED (CLARIFY, escalate after two), then TENTATIVE."""
    belief = t.belief
    if t.analysis.tiers_ambiguous:
        if t.neg.clarify_counts.get("min_payment_tiers", 0) >= 2:
            return _escalate(
                t.effects,
                reason="tiers_unresolved",
                spoken_reason="I need a specialist to confirm the payment tiers.",
            )
        return Action(
            intent=Intent.CLARIFY,
            effects=t.effects
            + [Effect(kind="note_clarify", data={"field": "min_payment_tiers"})],
            next_phase=Phase.DISCOVERY,
            reason="tiers_ambiguous",
            template_override=(
                "Just to be sure on the payment tiers: from which payment number "
                "does each higher minimum start, and what is that minimum?"
            ),
        )
    contradicted = belief.contradicted_fields()
    if contradicted:
        fname = contradicted[0]
        if t.neg.clarify_counts.get(fname, 0) >= 2:
            return _escalate(
                t.effects,
                reason="contradiction_unresolved",
                spoken_reason="I need a specialist to resolve conflicting terms.",
            )
        term = belief.get(fname)
        facts: dict[str, Fact] = {}
        slots = {"field_label": FIELDS_BY_NAME[fname].label}
        required: set[str] = set()
        old = term.history[-1] if term.history else None
        old_s = _spoken_value("clarify_old", fname, old, facts, slots, required)
        new_s = _spoken_value("clarify_new", fname, term.value, facts, slots, required)
        is_tiers = FIELDS_BY_NAME[fname].kind == "tiers"
        return Action(
            intent=Intent.CLARIFY,
            facts=facts,
            text_slots=slots,
            required=required,
            effects=t.effects + [Effect(kind="note_clarify", data={"field": fname})],
            next_phase=Phase.DISCOVERY,
            reason=fname,
            template_override=(
                f"Earlier you mentioned {old_s}, and now I am hearing {new_s}. "
                "Which of those should I use?"
                if is_tiers
                else None
            ),
        )
    tentative = belief.tentative_fields()
    if tentative:
        fname = tentative[0]
        facts = {}
        slots = {"field_label": FIELDS_BY_NAME[fname].label}
        required = set()
        value = belief.get(fname).value
        spoken = _spoken_value("readback_value", fname, value, facts, slots, required)
        effects = t.effects + [Effect(kind="set_pending_readback", data={"field": fname})]
        # A READ_BACK mid-CONFIRM keeps the phase, and an accept it preempted is
        # remembered so the readback "yes" wraps instead of reopening the ladder.
        neg, a = t.neg, t.analysis
        in_confirm = neg.phase == Phase.CONFIRM
        same_pct = a.settlement_ask_pct is None or (
            ask_pct_to_bp(a.settlement_ask_pct) == neg.confirmed_bp
        )
        if in_confirm and a.stance == "accept" and same_pct and neg.last_confirm_key:
            effects.append(
                Effect(kind="note_confirm_accepted", data={"key": list(neg.last_confirm_key)})
            )
        is_tiers = FIELDS_BY_NAME[fname].kind == "tiers"
        return Action(
            intent=Intent.READ_BACK,
            facts=facts,
            text_slots=slots,
            required=required,
            effects=effects,
            next_phase=Phase.CONFIRM if in_confirm else Phase.DISCOVERY,
            reason=fname,
            template_override=f"So I have {spoken}. Is that right?" if is_tiers else None,
        )
    return None


def _propose_wrap(t: _Turn) -> Action:
    """Send the confirmed schedule for client approval."""
    return Action(
        intent=Intent.PROPOSE_WRAP,
        facts=dict(t.confirm_facts or {}),
        effects=t.effects + [Effect(kind="set_phase", data={"phase": Phase.WRAP.value})],
        next_phase=Phase.WRAP,
        reason="confirmed",
    )


def _decide_confirm(t: _Turn) -> Action | None:
    """CONFIRM-phase accept of the unchanged schedule on the table → PROPOSE_WRAP.

    Not a wrap: ``readback_response``, a restated different % (a correction),
    a yes to a pending term alt, or a fingerprint that changed since CONFIRM.
    Exception: a confirmed READ_BACK after an accept it preempted
    (``accepted_confirm_key``) wraps if the fingerprint is unchanged, else
    re-confirms the accepted bp under the revised terms (never re-ladders).
    """
    a, neg = t.analysis, t.neg
    if neg.phase != Phase.CONFIRM or neg.confirmed_bp is None or neg.last_confirm_key is None:
        return None
    if (
        a.readback_response == "confirm"
        and neg.accepted_confirm_key == neg.last_confirm_key
        and a.settlement_ask_pct is None
        and a.stance not in ("reject", "counter", "offer")
    ):
        if _confirm_key(t.belief, neg.confirmed_bp) == neg.last_confirm_key:
            return _propose_wrap(t)
        afford = t.afford
        if (
            afford is not None
            and afford.max_bp is not None
            and neg.confirmed_bp <= accept_line_bp(afford.max_bp, t.cfg.accept_line_pct_of_max_bp)
            and neg.confirmed_bp in afford.feasible_bps
        ):
            return _confirm_action(
                ask_bp=neg.confirmed_bp,
                belief=t.belief,
                confirm_facts=t.confirm_facts,
                effects=t.effects,
                required=_confirm_required(t.confirm_facts),
                reason="terms_revised",
            )
    if a.stance != "accept" or t.accepted_term_alt:
        return None
    if (
        a.settlement_ask_pct is not None
        and ask_pct_to_bp(a.settlement_ask_pct) != neg.confirmed_bp
    ):
        return None
    if _confirm_key(t.belief, neg.confirmed_bp) != neg.last_confirm_key:
        return None
    return _propose_wrap(t)


def _decide_discovery(t: _Turn) -> Action | None:
    """ASK the next missing required field, then the settlement ask."""
    missing = t.belief.missing_required()
    if missing:
        return _ask_field(missing[0], t.effects)
    # afford is None only when rules are not buildable (missing fields caught above).
    if t.ask_bp is None or t.afford is None:
        return Action(
            intent=Intent.ASK_SETTLEMENT,
            effects=t.effects,
            next_phase=Phase.DISCOVERY,
        )
    return None


def _decide_negotiate(t: _Turn) -> Action:
    """Price and non-price moves once rules and the ask are known."""
    ask_bp, afford = t.ask_bp, t.afford
    assert ask_bp is not None and afford is not None
    if afford.max_bp is None:
        # Nothing feasible at this start date: FPD → min → max_pay alts, then rescue.
        alt = _term_alt_action(t)
        if alt is not None:
            return alt
        if t.rescue_within_guardrail:
            return _escalate(
                t.effects,
                reason="out_of_guardrail",
                spoken_reason="This needs client approval for extra funds before we continue.",
            )
        return _handoff(t.effects, "infeasible")
    # A further non-price alt that can raise the ceiling beats a price counter.
    if ask_bp > afford.max_bp:
        alt = _term_alt_action(t)
        if alt is not None:
            return alt
    accepted = _confirm_accepted_counter(t, ask_bp, afford)
    if accepted is not None:
        return accepted
    if ask_bp <= afford.max_bp and ask_bp in afford.feasible_bps:
        on_table = _reconfirm_on_table(t, ask_bp, afford)
        if on_table is not None:
            return on_table
    return _negotiate(t, ask_bp, afford)


def _term_alt_action(t: _Turn) -> Action | None:
    """COUNTER_TERMS for ``t.term_alt`` unless already offered (FPD gets one try)."""
    if t.term_alt is None:
        return None
    alt_field, alt_value = t.term_alt
    if terms_counter_key(alt_field, alt_value) in t.neg.terms_countered:
        return None
    if alt_field == "first_payment_date" and field_already_countered(
        t.neg.terms_countered, alt_field
    ):
        return None
    # Only object to a value the rep actually gave; an ASSUMED default is ours.
    rep_stated = t.belief.get(alt_field).status in _REP_STATED
    return _counter_terms_action(
        field=alt_field, value=alt_value, effects=t.effects, rep_stated=rep_stated
    )


# Belief statuses that carry a value the rep said (not our ASSUMED default).
_REP_STATED = frozenset({TermStatus.KNOWN, TermStatus.TENTATIVE, TermStatus.CONTRADICTED})


def _confirm_accepted_counter(t: _Turn, ask_bp: int, afford: Affordability) -> Action | None:
    """Rep accepted one of our offers: confirm it, if still feasible and at or below the line.

    A restated % counts only when it is a number we offered; any other number
    is the rep's own ask and goes through the ladder (never accepted first:
    otherwise "OK, 100% of the balance" would read as accepting our offer). A yes to a term alt is
    not a yes to the previous price — that counter can be a token percent the
    new terms just made obsolete.
    """
    a, neg = t.analysis, t.neg
    if a.stance != "accept":
        return None
    assert afford.max_bp is not None
    line = accept_line_bp(afford.max_bp, t.cfg.accept_line_pct_of_max_bp)
    legal = {bp for bp in afford.feasible_bps if bp <= line}
    ours = set(neg.counters_offered)
    if neg.confirmed_bp is not None:
        ours.add(neg.confirmed_bp)
    bp: int | None = None
    if a.settlement_ask_pct is not None:
        stated = ask_pct_to_bp(a.settlement_ask_pct)
        if stated not in ours:
            return None
        if stated in legal:
            bp = stated
    if bp is None and neg.counters_offered and not t.accepted_term_alt:
        if neg.counters_offered[-1] in legal:
            bp = neg.counters_offered[-1]
    if bp is None or (t.accepted_term_alt and bp < ask_bp):
        return None
    return _confirm_action(
        ask_bp=bp,
        belief=t.belief,
        confirm_facts=t.confirm_facts,
        effects=t.effects,
        required=_confirm_required(t.confirm_facts),
    )


def _reconfirm_on_table(t: _Turn, ask_bp: int, afford: Affordability) -> Action | None:
    """Affordable ask with a CONFIRM already spoken: re-confirm, stall, or wrap.

    Terms revised since CONFIRM → re-confirm at ``confirmed_bp``. Same schedule
    and it meets the ask → reject probes ASSUMED fields, accept wraps (phase-lag
    safety net), anything else soft-retries up to ``max_counters``. A confirm
    still below the ask, or a new lower ask, falls through to the ladder.
    """
    neg, a = t.neg, t.analysis
    confirmed = neg.confirmed_bp
    if confirmed is None or neg.last_confirm_key is None:
        return None
    assert afford.max_bp is not None
    new_ask = a.settlement_ask_pct is not None
    key_matches = _confirm_key(t.belief, confirmed) == neg.last_confirm_key
    if (
        not new_ask
        and not key_matches
        and confirmed <= accept_line_bp(afford.max_bp, t.cfg.accept_line_pct_of_max_bp)
        and confirmed in afford.feasible_bps
        and ask_bp <= confirmed
    ):
        return _confirm_action(
            ask_bp=confirmed,
            belief=t.belief,
            confirm_facts=t.confirm_facts,
            effects=t.effects,
            required=_confirm_required(t.confirm_facts),
            reason="terms_revised",
        )
    if ask_bp > confirmed or not key_matches or (new_ask and ask_bp < confirmed):
        return None
    if a.stance == "reject":
        return _stall_after_confirm(t)
    if a.stance == "accept":
        return _propose_wrap(t)
    soft = t.effects + [Effect(kind="inc_confirm_reject")]
    if neg.confirm_rejects + 1 >= t.cfg.max_counters:
        return _handoff(soft, "confirm_unacked")
    return _confirm_action(
        ask_bp=confirmed,
        belief=t.belief,
        confirm_facts=t.confirm_facts,
        effects=soft,
        required=set(),
    )


def _stall_after_confirm(t: _Turn) -> Action:
    """Rejected identical CONFIRM: verify each ASSUMED field once, else hand off."""
    effects = t.effects + [Effect(kind="inc_confirm_reject")]
    fname = _first_assumed_field(t.belief, already_asked=t.neg.assumed_asked)
    if fname is not None:
        return _ask_field(
            fname, effects + [Effect(kind="note_assumed_asked", data={"field": fname})]
        )
    return _handoff(effects, "confirm_rejected")
