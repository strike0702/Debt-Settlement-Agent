"""NLU: read one rep turn into verified acts the policy can dispatch on.

The model only reads; code decides what to trust. ``analyze`` (``llm_call``)
first tries the code-only ``fast_paths`` (a plain yes / no to a read-back, a
short pick between the two values of an open clarify, a bare answer to the
field we just asked), then makes one JSON ``nlu`` call that returns a typed
act list (``app.domain.nlu_types.ActList``) given a PUBLIC ``context`` of the
call. ``acts.verify_acts`` is the one verification path for model acts and
the simulator's oracle acts: rules and price through ``verify.post_verify``
(quote / number / range checks against ``app.agent.numbers``), every other act
through the ``cues`` repairs (accept / reject phrases, never accept on
injection, private-info and commitment cues, hostility floor, closing vs
"that's all", firmness, questions, announcements), and number accounting.
``amounts`` turns a verified dollar total into the ask once the balance is
known, or asks "total or per payment?". Rejected terms are audited and kept
on ``VerifiedAnalysis.dropped`` for the decision trace;
``VerifiedAnalysis.to_rep_turn()`` is the policy's input. Does not update
belief: the orchestrator does that via ``BeliefState.observe``.

Modules: ``models`` (result types), ``quotes`` (quote / value primitives),
``parse`` (tier shape), ``fast_paths``, ``cues``, ``verify``, ``amounts``,
``context`` (PUBLIC call state), ``acts`` (parse and verify acts),
``llm_call``. This package re-exports every public name (``__all__``), so
``from app.agent.nlu import X`` works for all of them.
"""

from app.agent.nlu.acts import (
    parse_acts,
    parse_tier_item,
    verify_acts,
)
from app.agent.nlu.amounts import (
    balance_statement_amount,
    bare_amount_ask,
    bp_to_ask_pct,
    resolve_amounts,
    total_cents_to_bp,
    total_shape_trigger,
    try_resolve_amount_clarify,
)
from app.agent.nlu.context import (
    CallContext,
    build_call_context,
)
from app.agent.nlu.cues import (
    HOSTILITY_FLOOR,
    hostility_floor_hit,
    repair_acts,
    repair_asks_client_private_info,
    repair_asks_for_schedule,
    repair_defines_field,
    repair_demands_commitment,
    repair_firm,
    repair_hostility,
    repair_list_done,
    repair_question,
    repair_revises_terms,
    repair_stance,
    repair_wants_to_end,
)
from app.agent.nlu.fast_paths import (
    try_fast_field_answer,
    try_fast_readback,
    try_resolve_cents_clarify,
    try_resolve_disputed_value,
)
from app.agent.nlu.llm_call import (
    analyze,
    overlay_oracle_acts,
)
from app.agent.nlu.models import (
    DroppedTerm,
    RawReading,
    RawTerm,
    VerifiedAnalysis,
    VerifiedTerm,
)
from app.agent.nlu.parse import (
    coerce_tiers,
)
from app.agent.nlu.quotes import (
    normalize_for_quote,
    quote_in_utterance,
)
from app.agent.nlu.verify import (
    post_verify,
    structure_word_ok,
)

__all__ = [
    "CallContext",
    "DroppedTerm",
    "HOSTILITY_FLOOR",
    "RawReading",
    "RawTerm",
    "VerifiedAnalysis",
    "VerifiedTerm",
    "analyze",
    "balance_statement_amount",
    "bare_amount_ask",
    "bp_to_ask_pct",
    "build_call_context",
    "coerce_tiers",
    "hostility_floor_hit",
    "normalize_for_quote",
    "overlay_oracle_acts",
    "parse_acts",
    "parse_tier_item",
    "post_verify",
    "repair_acts",
    "quote_in_utterance",
    "repair_asks_client_private_info",
    "repair_asks_for_schedule",
    "repair_demands_commitment",
    "repair_firm",
    "repair_hostility",
    "repair_defines_field",
    "repair_list_done",
    "repair_question",
    "repair_revises_terms",
    "repair_stance",
    "repair_wants_to_end",
    "resolve_amounts",
    "structure_word_ok",
    "total_cents_to_bp",
    "total_shape_trigger",
    "try_fast_field_answer",
    "try_fast_readback",
    "try_resolve_amount_clarify",
    "try_resolve_cents_clarify",
    "try_resolve_disputed_value",
    "verify_acts",
]

# Private helpers that eval tooling imports from this path; not public API.
from app.agent.nlu.cues import (  # noqa: E402, F401
    _INJECTION_RE,
    _ack_dominant,
    _has_unguarded_accept_phrase,
    _has_unguarded_reject_phrase,
)
