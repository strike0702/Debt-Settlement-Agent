"""NLU: read one rep turn into verified terms and flags the policy can act on.

The model only reads; code decides what to trust. ``analyze`` (``llm_call``)
first tries the code-only ``fast_paths`` (a plain yes / no to a read-back, a
short pick between the two values of an open clarify, a bare answer to the
field we just asked), then makes one JSON ``nlu`` call, with the last few
spoken turns as context (retried once on a validation failure) and parses it
(``parse``).
``verify.post_verify`` then drops bad quotes and out-of-range values and sets
``verified`` from ``app.agent.numbers.extract_tokens`` matches, and the
``cues`` repairs fix the stance (accept phrase, dominant short ack, never
accept on injection) and OR each side-channel flag with a regex cue. Those
code-side rules apply whatever the model returned: wider private-info cues, a
hostility floor for clear insults / threats, the off-script question flag only
with a question cue and never on a private-info ask (that one is refused, not
answered). ``amounts`` turns a verified dollar total into the ask once the
balance is known, or asks "total or per payment?" (no question on a balance
statement; always one for a bare demanded amount). ``NLU_MODE=oracle`` skips
the LLM and uses a caller-supplied ``TurnAnalysis`` (sim / tests). Rejected
terms are audited and kept on ``VerifiedAnalysis.dropped`` for the decision
trace. Does not update belief: the orchestrator does that via
``BeliefState.observe``.

Modules: ``models`` (result types), ``quotes`` (quote / value primitives),
``parse``, ``fast_paths``, ``cues``, ``verify``, ``amounts``, ``llm_call``.
This package re-exports every public name (``__all__``), so
``from app.agent.nlu import X`` works for all of them.
"""

from app.agent.nlu.amounts import (
    balance_statement_amount,
    bare_amount_ask,
    bp_to_ask_pct,
    resolve_amounts,
    total_cents_to_bp,
    total_shape_trigger,
    try_resolve_amount_clarify,
)
from app.agent.nlu.cues import (
    HOSTILITY_FLOOR,
    hostility_floor_hit,
    repair_asks_client_private_info,
    repair_asks_for_schedule,
    repair_demands_commitment,
    repair_firm,
    repair_hostility,
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
)
from app.agent.nlu.models import (
    DroppedTerm,
    VerifiedAnalysis,
    VerifiedTerm,
)
from app.agent.nlu.parse import (
    coerce_analysis_payload,
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
    "DroppedTerm",
    "HOSTILITY_FLOOR",
    "VerifiedAnalysis",
    "VerifiedTerm",
    "analyze",
    "balance_statement_amount",
    "bare_amount_ask",
    "bp_to_ask_pct",
    "coerce_analysis_payload",
    "coerce_tiers",
    "hostility_floor_hit",
    "normalize_for_quote",
    "post_verify",
    "quote_in_utterance",
    "repair_asks_client_private_info",
    "repair_asks_for_schedule",
    "repair_demands_commitment",
    "repair_firm",
    "repair_hostility",
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
]

# Private helpers that eval tooling imports from this path; not public API.
from app.agent.nlu.cues import (  # noqa: E402, F401
    _INJECTION_RE,
    _ack_dominant,
    _has_unguarded_accept_phrase,
    _has_unguarded_reject_phrase,
)
from app.agent.nlu.parse import (  # noqa: E402, F401
    _parse_analysis,
)
