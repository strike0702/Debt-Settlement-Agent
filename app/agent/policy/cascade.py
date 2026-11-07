"""``decide``: one move per rep turn, read from the turn's verified acts.

The rep's line arrives as a ``RepTurn`` (``app.domain.nlu_types``): the
verified acts of a closed set, with code-checked numbers. ``decide`` reads
them in ``DISPATCH_ORDER``, an order that follows what each act means rather
than a flag priority:

1. Call guard: the turn cap hands off whatever was said.
2. Safety acts: ``hostility`` (strong → hand-off), ``ask_private_info``
   (refuse, hand off on repeat), ``demand_commitment`` (same).
3. Call state: after a proposal (WRAP / END / ESCALATE) the wrap rules
   decide (``_decide_wrap``: after a hand-off or the end every line gets the
   terminal reply; in WRAP an unreadable line is asked again, a real change
   of price or terms hands off, anything else closes — WRAP never goes back
   to discovery or the ladder); a run
   of turns that added nothing hands off.
4. Acts that answer themselves: ``question`` (topic ``schedule`` after a
   proposal → read the schedule; other topics are answered by the act layer,
   ``app.agent.acts``, and the line goes on), ``end_call`` (an explicit
   goodbye / refusal / transfer: close on a schedule on the table, else
   follow up), ``price_ask`` (a number-free "final" while the line holds a
   number no act claimed → ask for the number, never reuse the old ask),
   ``unclear`` (ask to rephrase).
5. Acts folded into the state the phase flow reads: ``readback_reply`` (the
   orchestrator applied it to belief), ``answer`` / ``state_rule`` (belief),
   ``announce_rules`` / ``rule_item`` / ``list_done`` (the tier draft),
   ``respond_to_offer`` (confirm / ladder), ``stall`` / ``backchannel``.
   An act this phase cannot use (``list_done`` with no list open, a read-back
   reply with nothing read back) is never dropped silently: a line made only
   of such acts gets a clarify (``act_unusable``).
6. Phase flow: clarify / read-back of belief, rules the rep is defining,
   confirm, discovery, negotiate (the price ladder).

**Multi-act lines.** Acts are read in the order above, whatever their order
in the line; the first act whose handler returns a move decides, and every
other act has already been folded into belief or the draft (so "Payments
three to five at least $250, and that's all" adds the tier and closes the
list in one move). Conflicts, written once:

- an accept outranks a goodbye in the same line ("Great, thanks") and a
  schedule cue ("the payment schedule works");
- "that's all" while a list is open closes the list, not the call (the NLU
  drops the ``end_call``; ``_read_rule_replies`` keeps the rule after the sweep);
- an accept that restates a different number is that number, not a yes
  (CONFIRM wraps only on an accept with no new price);
- two prices in one line: the later one (a self-correction);
- ``unclear`` with any other surviving act is dropped by the NLU.

After the move, ``loop_guard`` hands off instead of asking the same question
a third time and rewords a question the rep just did not answer;
``rep_turn_progress`` tells the orchestrator whether the turn added anything.
Every move carries a reason code (``app.agent.reasons.reason_key`` folds a
missing one into its intent's code). Pure: no I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Callable
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
    rephrased_ask,
    sweep_action,
    tier_ask_action,
    tier_conflict,
    tier_conflict_clarify_action,
    tier_draft_effect,
    tier_readback_action,
    yield_ask_action,
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
from app.domain.nlu_types import (
    ACT_TYPES,
    AnnounceRulesAct,
    ListDoneAct,
    RepTurn,
    RespondToOfferAct,
    RuleItemAct,
)

# Moves that ask the rep something; the loop guard counts them per key.
_QUESTION_INTENTS = frozenset(
    {Intent.ASK, Intent.ASK_SETTLEMENT, Intent.CLARIFY, Intent.READ_BACK}
)
_TIERS = "min_payment_tiers"
_SWEEP_KEY = "ASK:optional_sweep"


def question_key(action: Action) -> str | None:
    """``INTENT:reason`` for a question move (same intent + field / template), else None."""
    if action.intent not in _QUESTION_INTENTS:
        return None
    key = f"{action.intent.value}:{action.reason or ''}"
    # Tier asks differ by how far the list got ("next tier" after the 1st and
    # after the 2nd are different questions): key on draft size / expected count.
    draft = next((e for e in action.effects if e.kind == "set_tier_draft"), None)
    if action.intent == Intent.ASK and draft is not None:
        key += f":{len(draft.data.get('tiers') or [])}/{draft.data.get('expected')}"
    return key


def loop_guard(action: Action, neg: NegotiationState, cfg: Settings) -> Action:
    """Hand off instead of asking the same question a third time; else count this ask.

    The count is an ``note_question`` effect, applied when the move is spoken.
    An ASK / ASK_SETTLEMENT that repeats the question we asked last, after a
    rep turn that added nothing (``neg.no_progress_turns``), is reworded
    (``rephrased_ask``) so the same line is never spoken twice in a row.
    """
    key = question_key(action)
    # ``_decide_wrap`` re-enters ``decide``; count each spoken question once.
    if key is None or any(e.kind == "note_question" for e in action.effects):
        return action
    if action.intent == Intent.READ_BACK and action.reason in neg.unconfirmed_fields:
        return action  # read-back of an unclear number: not counted (user decision)
    if neg.question_counts.get(key, 0) >= cfg.max_same_question:
        kept = [e for e in action.effects if e.kind in ("record_ask", "clear_pending_terms_alt")]
        return _handoff(kept, "repeated_question")
    # The rep just answered something else: same question, other words (still counted).
    if (
        key == neg.last_question_key
        and neg.no_progress_turns > 0
        and action.intent in (Intent.ASK, Intent.ASK_SETTLEMENT)
        and action.template_override is None
    ):
        action = rephrased_ask(action)
    return action.model_copy(
        update={"effects": [*action.effects, Effect(kind="note_question", data={"key": key})]}
    )


def rep_turn_progress(
    turn: RepTurn,
    neg: NegotiationState,
    belief_changes: list[BeliefChange],
    *,
    accepted_term_alt: bool = False,
) -> bool:
    """True when this rep turn added verified information or moved the call.

    Progress: a belief value or status changed, a new ask, an accept or reject,
    a read-back answer, a firm price, a yes to a non-price alternative, or a
    rule announcement / tier for the tier draft / the end of the list.
    A restated ask, a repeated term, a question or small talk is not progress.
    """
    if any(
        c.old_value != c.new_value or c.old_status != c.new_status for c in belief_changes
    ):
        return True
    if turn.ask_pct is not None and ask_pct_to_bp(turn.ask_pct) != neg.ask_bp:
        return True
    return (
        turn.accepts
        or turn.rejects
        or turn.readback is not None
        or turn.firm
        or accepted_term_alt
        # Rule announcements and tiers held in the draft (not belief yet).
        or turn.announced is not None
        or turn.list_done
        or bool(turn.tier_items())
    )


def decide(
    belief: BeliefState,
    neg: NegotiationState,
    turn: RepTurn,
    afford: Affordability | None,
    *,
    context: Any = None,
    settings: Settings | None = None,
    rescue_within_guardrail: bool = False,
    confirm_facts: dict[str, Fact] | None = None,
    counter_offer_total_cents: int | None = None,
    term_alt: tuple[str, Any] | None = None,
    accepted_term_alt: bool = False,
) -> Action:
    """Return the next agent ``Action`` for the rep's verified acts (module docstring).

    ``context`` is the PUBLIC ``CallContext`` built before the turn (what we
    were awaiting); when omitted it is built from ``belief`` / ``neg``.
    ``term_alt`` is ``(field, value)`` for the next non-price recovery stage
    when the curve is empty or the ask sits above the ceiling
    (first_payment_date → min_payment → max_payments). ``accepted_term_alt`` is
    true when this turn's accept applied a pending non-price alternative; that
    yes is not acceptance of the last price.
    """
    if context is None:
        from app.agent.nlu.context import build_call_context

        context = build_call_context(belief, neg)
    ask_bp = neg.ask_bp
    effects: list[Effect] = []
    if turn.ask_pct is not None:
        ask_bp = ask_pct_to_bp(turn.ask_pct)
        effects.append(Effect(kind="record_ask", data={"bp": ask_bp}))
    # A rejected pending non-price alt is cleared so the cascade offers the next stage.
    if neg.pending_terms_alt is not None and turn.rejects and turn.ask_pct is None:
        effects.append(Effect(kind="clear_pending_terms_alt", data={}))
        neg = replace(neg, pending_terms_alt=None)

    turn = _read_rule_replies(turn, neg)

    t = _Turn(
        belief=belief,
        neg=neg,
        turn=turn,
        ctx=context,
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
    t = _with_tier_draft(t)
    action = _dispatch(t)
    return loop_guard(action, neg, t.cfg)


# --- act dispatch -------------------------------------------------------------

_Handler = Callable[[_Turn], Action | None]


def _dispatch(t: _Turn) -> Action:
    """Run ``DISPATCH_ORDER``; the first step that returns a move decides."""
    for _name, step in DISPATCH_ORDER:
        if step is None:
            continue
        action = step(t)
        if action is not None:
            return action
    unusable = _unusable_line(t)
    if unusable is not None:
        return unusable
    for phase_step in (_decide_clarify, _decide_rep_rules, _decide_confirm, _decide_discovery):
        action = phase_step(t)
        if action is not None:
            return action
    return _decide_negotiate(t)


def _stay(neg: NegotiationState) -> Phase:
    return neg.phase if neg.phase != Phase.OPENING else Phase.DISCOVERY


def _max_turns(t: _Turn) -> Action | None:
    if t.neg.turn_idx > t.cfg.max_turns:
        return _handoff(t.effects, "max_turns")
    return None


def _on_hostility(t: _Turn) -> Action | None:
    """Strong hostility (score at the threshold) hands off; mild is ignored."""
    if t.turn.hostility() >= t.cfg.hostility_threshold:
        return _escalate(
            t.effects,
            reason="hostile",
            spoken_reason="The tone on this call needs a specialist.",
        )
    return None


def _on_private_info(t: _Turn) -> Action | None:
    """Refuse a question about the client's money once; hand off on the second."""
    if not t.turn.has("ask_private_info"):
        return None
    bumped = t.effects + [Effect(kind="inc_private_ask")]
    if t.neg.private_ask_count >= 1:
        return _escalate(
            bumped,
            reason="sensitive_request",
            spoken_reason="I need to hand this off after a sensitive request.",
        )
    return Action(
        intent=Intent.REFUSE_PRIVATE, effects=bumped, next_phase=_stay(t.neg), reason="private_info"
    )


def _on_commitment(t: _Turn) -> Action | None:
    """Refuse a lock-in demand once; hand off on the second."""
    if not t.turn.has("demand_commitment"):
        return None
    bumped = t.effects + [Effect(kind="inc_commit_demand")]
    if t.neg.commit_demand_count >= 1:
        return _escalate(
            bumped,
            reason="commitment_demand",
            spoken_reason="I need to involve someone about a commitment demand.",
        )
    return Action(
        intent=Intent.REFUSE_COMMIT, effects=bumped, next_phase=_stay(t.neg), reason="commitment"
    )


def _post_proposal(t: _Turn) -> Action | None:
    if t.neg.phase in (Phase.WRAP, Phase.END, Phase.ESCALATE):
        return _decide_wrap(t)
    return None


def _no_progress(t: _Turn) -> Action | None:
    if t.neg.no_progress_turns >= t.cfg.max_no_progress_turns:
        return _handoff(t.effects, "no_progress")
    return None


def _on_question(t: _Turn) -> Action | None:
    """Schedule detail after a proposal: read it (an accept outranks the cue).

    Other topics fall through: the act layer answers them before the move.
    """
    tu = t.turn
    if tu.asks("schedule") and t.neg.last_confirm_key is not None and not tu.accepts:
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=t.effects,
            next_phase=Phase.CONFIRM,
            reason="schedule_detail",
        )
    return None


def _on_end_call(t: _Turn) -> Action | None:
    """An explicit goodbye / refusal / transfer (an accept in the line outranks it)."""
    if not t.turn.ends or t.turn.accepts:
        return None
    neg = t.neg
    # Soft-accept a schedule already on the table, then close with thanks.
    if neg.phase == Phase.CONFIRM and neg.last_confirm_key is not None:
        return Action(
            intent=Intent.CLOSE,
            facts=dict(t.confirm_facts or {}),
            effects=t.effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
            next_phase=Phase.END,
            reason="thanks_accept",
        )
    return follow_up_action(t.effects)


# Number-free rephrase asks for the two "cannot act" signals.
_UNREAD_NUMBER_TEMPLATE = (
    "Sorry, I want to get your number right. What is the lowest settlement you can accept?"
)
_UNCLEAR_TEMPLATE = "Sorry, I did not quite catch that. Could you say it another way?"
_UNUSABLE_TEMPLATE = "Sorry, I am not sure what that refers to. Could you say a bit more?"


def _on_price_ask(t: _Turn) -> Action | None:
    """A final price whose number went unread: ask for it, never reuse the older ask.

    "I think seventy three is the lowest I can go" with the 73 unclaimed would
    otherwise accept the standing 75. A price with a verified number goes to
    the ladder (phase flow).
    """
    tu = t.turn
    if tu.firm and tu.unaccounted_numbers and tu.ask_pct is None:
        return Action(
            intent=Intent.CLARIFY,
            effects=t.effects,
            next_phase=_stay(t.neg),
            reason="unaccounted_number",
            template_override=_UNREAD_NUMBER_TEMPLATE,
        )
    return None


def _on_unclear(t: _Turn) -> Action | None:
    if t.turn.unclear:
        return Action(
            intent=Intent.CLARIFY,
            effects=t.effects,
            next_phase=_stay(t.neg),
            reason="unclear",
            template_override=_UNCLEAR_TEMPLATE,
        )
    return None


# Steps in reading order: ``(act type or gate name, handler)``. A ``None``
# handler means the act is folded into state and the phase flow answers it.
DISPATCH_ORDER: tuple[tuple[str, _Handler | None], ...] = (
    ("turn_cap", _max_turns),
    ("hostility", _on_hostility),
    ("ask_private_info", _on_private_info),
    ("demand_commitment", _on_commitment),
    ("post_proposal", _post_proposal),
    ("no_progress", _no_progress),
    ("question", _on_question),
    ("end_call", _on_end_call),
    ("price_ask", _on_price_ask),
    ("unclear", _on_unclear),
    ("readback_reply", None),
    ("answer", None),
    ("state_rule", None),
    ("announce_rules", None),
    ("rule_item", None),
    ("list_done", None),
    ("respond_to_offer", None),
    ("stall", None),
    ("backchannel", None),
)
# Every act type has a place in the order (the grid test checks the same).
assert set(ACT_TYPES) <= {name for name, _ in DISPATCH_ORDER}


def _usable(act: Any, t: _Turn) -> bool:
    """False for an act this phase has no use for (the rest are always read)."""
    if act.type == "list_done":
        return t.ctx.collecting is not None or t.neg.tier_draft is not None
    if act.type == "readback_reply":
        return (
            t.ctx.awaiting == "readback"
            or t.neg.phase == Phase.CONFIRM
            or getattr(act, "correction", None) is not None
        )
    return True


def _unusable_line(t: _Turn) -> Action | None:
    """CLARIFY when every act in the line is one this phase cannot use."""
    acts = t.turn.acts
    if not acts or any(_usable(a, t) for a in acts):
        return None
    return Action(
        intent=Intent.CLARIFY,
        effects=t.effects,
        next_phase=_stay(t.neg),
        reason="act_unusable",
        template_override=_UNUSABLE_TEMPLATE,
    )


# --- post-proposal --------------------------------------------------------------


def _close_after_wrap(effects: list[Effect], *, reason: str) -> Action:
    """Polite thanks after a proposal was already sent to the client."""
    return Action(
        intent=Intent.CLOSE,
        effects=effects + [Effect(kind="set_phase", data={"phase": Phase.END.value})],
        next_phase=Phase.END,
        reason=reason,
    )


def _changes_terms(t: _Turn) -> bool:
    """A real change request after the proposal: a new price, a rule value that
    differs from belief, tiers, or an announced rule list.

    A bare reject ("No.") is not one: after "anything else?" it means "nothing".
    """
    tu = t.turn
    if tu.ask_pct is not None:
        agreed = t.neg.confirmed_bp if t.neg.confirmed_bp is not None else t.neg.ask_bp
        if agreed is None or round(tu.ask_pct * 100) != agreed:
            return True
    if any(a.value != t.belief.get(a.field).value for a in tu.terms):
        return True
    return bool(tu.tier_items()) or tu.announced is not None


def _decide_wrap(t: _Turn) -> Action:
    """Post-proposal phases: never reopen discovery or the ladder (71c).

    WRAP allows only: read the schedule (a question), ask again on an
    unreadable line, hand off on a real change request (the proposal no longer
    matches the rep's terms), else CLOSE ("No", empty, a reject, a goodbye).
    The safety refusals run before this step.
    """
    tu, neg = t.turn, t.neg
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
    if tu.asks("schedule") and not tu.accepts:
        return Action(
            intent=Intent.SPEAK_SCHEDULE,
            effects=t.effects,
            next_phase=Phase.WRAP,
            reason="schedule_detail_post_wrap",
        )
    if tu.unclear:
        # Proposal sent and the reply could not be read: ask, never close on it.
        return Action(
            intent=Intent.CLARIFY,
            effects=t.effects,
            next_phase=Phase.WRAP,
            reason="unclear",
            template_override=_UNCLEAR_TEMPLATE,
        )
    if _changes_terms(t):
        # The drafted agreement no longer matches the rep's terms.
        return _handoff(t.effects + [Effect(kind="clear_wrap", data={})], "changed_after_proposal")
    return _close_after_wrap(t.effects, reason="post_wrap")


# --- phase flow -------------------------------------------------------------------

_TIERS_CLARIFY_TEMPLATE = (
    "Just to be sure on the payment tiers: from which payment number "
    "does each higher minimum start, and what is that minimum?"
)


def _decide_clarify(t: _Turn) -> Action | None:
    """Ambiguous tiers, then CONTRADICTED (CLARIFY, escalate after two), then TENTATIVE."""
    belief = t.belief
    if t.turn.tiers_ambiguous or t.tier_gap:
        if t.neg.clarify_counts.get(_TIERS, 0) >= 2:
            return _escalate(
                t.effects,
                reason="tiers_unresolved",
                spoken_reason="I need a specialist to confirm the payment tiers.",
            )
        return Action(
            intent=Intent.CLARIFY,
            effects=t.effects + [Effect(kind="note_clarify", data={"field": _TIERS})],
            next_phase=Phase.DISCOVERY,
            reason="tiers_ambiguous" if t.turn.tiers_ambiguous else "tiers_gap",
            template_override=_TIERS_CLARIFY_TEMPLATE,
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
        neg, tu = t.neg, t.turn
        in_confirm = neg.phase == Phase.CONFIRM
        same_pct = tu.ask_pct is None or ask_pct_to_bp(tu.ask_pct) == neg.confirmed_bp
        if in_confirm and tu.accepts and same_pct and neg.last_confirm_key:
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


def _read_rule_replies(turn: RepTurn, neg: NegotiationState) -> RepTurn:
    """Read short replies in the light of our last rule question.

    After the sweep ("Any tiered minimums, or a limit on payment levels?") a
    bare yes means "there are tiers" (start collecting them) and a bare no or
    "that's all" is information, not a rejection of a price. While tiers are
    being listed, "that's all" ends the list, not the call.
    """
    bare = (
        not turn.terms
        and not turn.of(RuleItemAct)
        and turn.ask_pct is None
        and turn.announced is None
    )
    if neg.last_question_key == _SWEEP_KEY and bare:
        if turn.accepts:
            kept = [a for a in turn.acts if not isinstance(a, RespondToOfferAct)]
            ann = AnnounceRulesAct(type="announce_rules", field=_TIERS, quote="tiers")
            return turn.model_copy(update={"acts": [*kept, ann]})
        if turn.rejects or turn.list_done:
            kept = [
                a for a in turn.acts if not isinstance(a, (RespondToOfferAct, ListDoneAct))
            ]
            return turn.model_copy(update={"acts": kept})
    if neg.tier_draft is not None and turn.list_done and turn.ends:
        return turn.model_copy(update={"acts": [a for a in turn.acts if a.type != "end_call"]})
    return turn


def _gap(ends: dict[int, int], draft: list[tuple[int, int]]) -> bool:
    """True when a drafted range ends somewhere other than just before the next tier."""
    starts = [frm for frm, _ in draft]
    for frm, nxt in zip(starts, starts[1:], strict=False):
        end = ends.get(frm)
        if end is not None and nxt != end + 1:
            return True
    return False


def _with_tier_draft(t: _Turn) -> _Turn:
    """Merge this turn's tiers into the draft the rep is giving one by one.

    Collecting starts when the rep announces tiers (``announce_rules``) and
    ends when the expected count is reached, the rep says that's all (or no),
    or the rep moves on (a price or another rule): then the set is read back
    once. A tier whose range leaves a gap or overlaps a drafted one (ends are
    kept across turns) is not merged and is asked again (``tiers_gap``).
    ``set_tier_draft`` on every move of the turn records the merge.
    """
    tu, neg = t.turn, t.neg
    ann = tu.announced
    announces_tiers = ann is not None and ann.field == _TIERS
    starting = neg.tier_draft is None and announces_tiers
    if neg.tier_draft is None and not starting:
        return t
    items = tu.tier_items()
    if not items and neg.tier_pending is not None:
        items = _pending_tier_answer(tu, neg.tier_pending)
    new = [(frm, cents) for frm, _, cents in items]
    merged = dict(neg.tier_draft or [])
    merged.update(new)  # a tier restated from the same payment replaces the old one
    ends = dict(neg.tier_ends)
    for frm, to, _ in items:
        if to is not None:
            ends[frm] = to
        else:
            ends.pop(frm, None)
    gap = bool(new) and _gap(ends, sorted(merged.items()))
    if gap:
        merged, ends, new = dict(neg.tier_draft or []), dict(neg.tier_ends), []
    draft = sorted(merged.items())
    expected = (
        ann.count if announces_tiers and ann is not None and ann.count is not None
        else neg.tier_expected
    )
    moved_on = (
        not new
        and not gap
        and not starting
        and not announces_tiers
        and (tu.ask_pct is not None or any(x.field != _TIERS for x in tu.terms))
    )
    # A no to the read-back (the orchestrator cleared the draft) is not "no more tiers".
    said_no_more = tu.readback != "deny" and (
        tu.list_done or (bool(draft) and not new and tu.rejects)
    )
    done = not gap and (said_no_more or moved_on)
    readback = bool(draft) and (done or (expected is not None and len(draft) >= expected))
    tiers: list[tuple[int, int]] | None = None if done and not draft else draft
    effect = tier_draft_effect(tiers, expected, readback=readback, ends=ends)
    return replace(
        t,
        neg=replace(
            neg, tier_draft=tiers, tier_expected=expected, tier_readback=readback, tier_ends=ends
        ),
        effects=[*t.effects, effect],
        tier_gap=gap,
    )


def _pending_tier_answer(
    turn: RepTurn, pending: tuple[int, int | None, int]
) -> list[tuple[int, int | None, int]]:
    """The tier a conflict clarify asked about, with the minimum the rep now gives.

    "I mean, one eighty dollars." after "...that tier's minimum is $100. Which
    minimum applies?" is a minimum (``min_payment_cents``) for that tier, not a
    new tier list: keep its range and continue with the next tier (71c).
    """
    frm, to, _ = pending
    for act in turn.terms:
        if act.field == "min_payment_cents" and isinstance(act.value, int) and act.value > 0:
            return [(frm, to, act.value)]
    return []


def _rule_value(belief: BeliefState, field: str) -> int | None:
    value = belief.get(field).value
    return value if isinstance(value, int) else None


def _decide_rep_rules(t: _Turn) -> Action | None:
    """Yield to a rep who wants to define a rule: ask for it before the price.

    Tiers: ask for the first / next tier, clarify one that contradicts the
    max payments or the minimum payment (escalate after two), and read the
    whole set back once it is complete. Another field: ask its question.
    """
    tu, neg = t.turn, t.neg
    if neg.tier_draft is not None:
        draft = neg.tier_draft
        max_p = _rule_value(t.belief, "max_payments")
        min_c = _rule_value(t.belief, "min_payment_cents")
        conflict = tier_conflict(draft, max_payments=max_p, min_payment_cents=min_c)
        if conflict is not None:
            if neg.clarify_counts.get(_TIERS, 0) >= 2:
                return _escalate(
                    t.effects,
                    reason="tiers_unresolved",
                    spoken_reason="I need a specialist to confirm the payment tiers.",
                )
            kind, tier = conflict
            rest = [x for x in draft if x != tier]
            ends = {k: v for k, v in neg.tier_ends.items() if k != tier[0]}
            pending = (tier[0], neg.tier_ends.get(tier[0]), tier[1])
            effects = [e for e in t.effects if e.kind != "set_tier_draft"]
            effects.append(
                tier_draft_effect(rest, neg.tier_expected, ends=ends, pending=pending)
            )
            rule = max_p if kind == "past_max" else min_c
            assert rule is not None
            return tier_conflict_clarify_action(kind, tier, rule_value=rule, effects=effects)
        if neg.tier_readback:
            return tier_readback_action(tiers=draft, max_payments=max_p, effects=t.effects)
        again = not draft and tu.readback == "deny"
        return tier_ask_action(
            draft=draft, expected=neg.tier_expected, effects=t.effects, again=again
        )
    ann = tu.announced
    if ann is not None and ann.field != _TIERS and all(x.field != ann.field for x in tu.terms):
        return yield_ask_action(ann.field, t.effects)
    return None


def _sweep_due(t: _Turn) -> bool:
    """Ask once about tiers / payment levels before the price, flexible schedules only.

    Skipped when the rep already gave either rule, and once a price is on the
    table (a counter or a confirm), so it never interrupts the bargaining.
    """
    b, neg = t.belief, t.neg
    return (
        not neg.optional_sweep_done
        and b.get("payment_structure").value == "flexible"
        and b.usable_for_engine("payment_structure")
        and b.get(_TIERS).status == TermStatus.ASSUMED
        and b.get("max_segments").status == TermStatus.ASSUMED
        and not neg.counters_offered
        and neg.confirmed_bp is None
    )


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

    Not a wrap: a read-back reply, a restated different % (a correction), a
    yes to a pending term alt, or a fingerprint that changed since CONFIRM.
    Exception: a confirmed READ_BACK after an accept it preempted
    (``accepted_confirm_key``) wraps if the fingerprint is unchanged, else
    re-confirms the accepted bp under the revised terms (never re-ladders).
    """
    tu, neg = t.turn, t.neg
    if neg.phase != Phase.CONFIRM or neg.confirmed_bp is None or neg.last_confirm_key is None:
        return None
    if (
        tu.readback == "confirm"
        and neg.accepted_confirm_key == neg.last_confirm_key
        and tu.ask_pct is None
        and not (tu.rejects or tu.price is not None)
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
    if not tu.accepts or t.accepted_term_alt:
        return None
    if tu.ask_pct is not None and ask_pct_to_bp(tu.ask_pct) != neg.confirmed_bp:
        return None
    if _confirm_key(t.belief, neg.confirmed_bp) != neg.last_confirm_key:
        return None
    return _propose_wrap(t)


def _decide_discovery(t: _Turn) -> Action | None:
    """ASK the next missing required field, the optional sweep, then the settlement ask."""
    missing = t.belief.missing_required()
    if missing:
        return _ask_field(missing[0], t.effects)
    if _sweep_due(t):
        return sweep_action(t.effects)
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
    tu, neg = t.turn, t.neg
    if not tu.accepts:
        return None
    assert afford.max_bp is not None
    line = accept_line_bp(afford.max_bp, t.cfg.accept_line_pct_of_max_bp)
    legal = {bp for bp in afford.feasible_bps if bp <= line}
    ours = set(neg.counters_offered)
    if neg.confirmed_bp is not None:
        ours.add(neg.confirmed_bp)
    bp: int | None = None
    if tu.ask_pct is not None:
        stated = ask_pct_to_bp(tu.ask_pct)
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
    neg, tu = t.neg, t.turn
    confirmed = neg.confirmed_bp
    if confirmed is None or neg.last_confirm_key is None:
        return None
    assert afford.max_bp is not None
    new_ask = tu.ask_pct is not None
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
    if tu.rejects:
        return _stall_after_confirm(t)
    if tu.accepts:
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
