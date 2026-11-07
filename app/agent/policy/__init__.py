"""Pure synchronous negotiation policy: one ``Action`` per rep turn.

``decide`` maps belief + the rep's verified acts (``RepTurn``) + the PUBLIC
call context + Affordability to one ``Action``. No I/O, no LLM; spoken
figures ride in PUBLIC ``Action.facts`` and side effects in ``Action.effects``
(the orchestrator applies them on speech ack). Acts are read in
``cascade.DISPATCH_ORDER``, an order by meaning (safety acts, the call state,
acts that answer themselves, then acts folded into belief / the tier draft),
and the phase flow answers the rest:

1. clarify: tiers that cannot be placed / CONTRADICTED → CLARIFY (escalate
   after two); TENTATIVE → READ_BACK.
2. rep rules: the rep announced a rule → ask it ("Go ahead. ..."); tiers
   given one by one → ask the next, clarify one that contradicts max payments
   / minimum or leaves a gap, read the whole set back once.
3. confirm: accept of the unchanged CONFIRM on the table → PROPOSE_WRAP.
4. discovery: ASK a missing required field, the optional sweep (flexible
   schedules only), then ASK_SETTLEMENT.
5. negotiate: empty curve (term alt / rescue / handoff), term alt over an
   unreachable ask, accepted counter, the price ladder (``_negotiate``).

After the move, ``loop_guard`` turns a question asked twice already into a
handoff (``repeated_question``) and rewords a question the rep just did not
answer, so no line is spoken twice in a row.

Call endings: a call ends only as a confirmed deal (PROPOSE_WRAP /
``thanks_accept``) or a handoff (ESCALATE, with a reason code for the person
taking over). ``decide`` never emits NO_DEAL_WRAP.

Modules: ``state`` (NegotiationState, Agreement, bp helpers), ``moves`` (Action
builders), ``ladder`` (the price ladder, described in its docstring),
``cascade`` (``decide``, the act dispatch and the loop guard). Every public
name is importable from ``app.agent.policy``.
"""

from app.agent.policy.cascade import (
    DISPATCH_ORDER,
    decide,
    loop_guard,
    question_key,
    rep_turn_progress,
)
from app.agent.policy.ladder import anchor_bp, step_bp
from app.agent.policy.moves import (
    amount_meaning_clarify_action,
    amount_meaning_escalate_action,
    opening_action,
)
from app.agent.policy.state import (
    Agreement,
    NegotiationState,
    ask_pct_to_bp,
    draft_agreement,
    field_already_countered,
    parse_pending_terms_value,
    terms_counter_key,
)
from app.domain.actions import Action, Effect, Intent, Phase

# Re-export for callers that import Action/Intent/Phase from policy.
__all__ = [
    "Action",
    "Agreement",
    "DISPATCH_ORDER",
    "Effect",
    "Intent",
    "NegotiationState",
    "Phase",
    "amount_meaning_clarify_action",
    "amount_meaning_escalate_action",
    "anchor_bp",
    "ask_pct_to_bp",
    "decide",
    "draft_agreement",
    "field_already_countered",
    "loop_guard",
    "opening_action",
    "parse_pending_terms_value",
    "question_key",
    "rep_turn_progress",
    "step_bp",
    "terms_counter_key",
]

# Also importable from here (not in ``__all__``): the remaining move builders,
# and two private helpers that tests call directly.
from app.agent.policy.moves import (  # noqa: E402, F401
    AMOUNT_CLARIFY_KEY,
    HANDOFF_SPOKEN,
    _confirm_key,
    _counter_terms_action,
    cents_ambiguity_clarify_action,
    follow_up_action,
    speak_schedule_action,
)
