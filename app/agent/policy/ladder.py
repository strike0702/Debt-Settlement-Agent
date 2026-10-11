"""Price ladder (``_negotiate``): how the agent haggles over the settlement percentage.

The *accept line* is 75% of the client's ceiling (``accept_line_bp``, rounded
down); we never offer above it and accept only at or below it, and only at a
bp the engine can schedule.

- The rep's first number is never accepted: we counter at the anchor
  (``anchor_ratio`` × min(ask, line)). Only when no lower legal counter exists
  may we accept it (``no_lower_counter``).
- Rep moved down since our last counter: we concede half their move
  (``concession_factor``), and the hold / step count resets.
- Rep did not move: hold once (restate our offer), then two equal steps that
  split the gap between our held offer and min(ask, line), the second landing on
  it; then accept if their ask is at or
  below the line (``rep_held``), else hand off (``above_accept_line``).
- Rep firm: above the line → hand off now; else one final counter halfway
  between our last offer and their number, and accept when they repeat it
  (``rep_firm``).
- ``max_counters`` spoken counters (holds included) caps the ladder: accept at
  or below the line (``counters_exhausted``), else hand off (``max_counters``).

Called from the last step of ``cascade._decide_negotiate``; ``anchor_bp`` and
``step_bp`` are the ladder arithmetic (integer bp).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.adapter.engine_adapter import Affordability
from app.agent.policy.moves import _confirm_action, _handoff
from app.agent.policy.state import _Turn
from app.domain.actions import Action, Effect, Intent, Phase
from app.domain.facts import Fact
from app.domain.negotiation import accept_line_bp


def anchor_bp(*, ask_bp: int, line_bp: int, legal: list[int], anchor_ratio: float) -> int | None:
    """Opening counter: highest legal bp ≤ ``anchor_ratio`` × min(ask, line), below the ask.

    ``legal`` is the feasible grid at or below the accept line. Falls back to
    the lowest legal bp below the ask; ``None`` when nothing legal is below it.
    """
    below = [bp for bp in legal if bp < ask_bp]
    if not below:
        return None
    cap = int(Decimal(str(anchor_ratio)) * Decimal(min(ask_bp, line_bp)))
    under = [bp for bp in below if bp <= cap]
    return max(under) if under else min(below)


def step_bp(*, prev: int, raw: int, ceiling: int, legal: list[int]) -> int | None:
    """Next counter above ``prev``: highest legal bp ≤ ``raw``, else the next legal bp up.

    Never above ``ceiling`` (exclusive bound = the rep's ask, or the line + 1).
    ``None`` when no legal bp lies strictly between ``prev`` and ``ceiling``.
    """
    room = [bp for bp in legal if prev < bp < ceiling]
    if not room:
        return None
    under = [bp for bp in room if bp <= raw]
    return max(under) if under else min(room)


# COUNTER templates for the price-ladder moves; ``{offer_total}`` is required
# because ``Orchestrator._enrich_action`` makes it so for every COUNTER.
_HOLD_TEMPLATE = (
    "We are staying at {counter_pct} of the balance, which is {offer_total}. "
    "Can you come down from your number?"
)
_FINAL_COUNTER_TEMPLATE = (
    "We can meet you partway at {counter_pct} of the balance, which is "
    "{offer_total}. Would that work?"
)


def _counter_action(
    c_next: int,
    effects: list[Effect],
    *,
    counter_offer_total_cents: int | None = None,
    ask_bp: int | None = None,
    turn: int | None = None,
    stage: int = 0,
    final_ask: int | None = None,
    reason: str | None = None,
    template: str | None = None,
) -> Action:
    """Emit COUNTER at ``c_next`` with optional spoken offer total.

    ``offer_counter`` carries the ladder bookkeeping (rep's ask now, hold / step
    stage after this move, final-counter ask, turn), applied idempotently on
    eager emit and again on speech ack.
    """
    facts: dict[str, Fact] = {
        "counter_pct": Fact(
            id="counter_pct",
            kind="pct",
            value=c_next,
            visibility="PUBLIC",
            source="engine",
        ),
    }
    if counter_offer_total_cents is not None:
        facts["offer_total"] = Fact(
            id="offer_total",
            kind="money",
            value=counter_offer_total_cents,
            visibility="PUBLIC",
            source="engine",
        )
    data: dict[str, Any] = {"bp": c_next, "stage": stage}
    if ask_bp is not None:
        data["ask_bp"] = ask_bp
    if turn is not None:
        data["turn"] = turn
    if final_ask is not None:
        data["final_ask"] = final_ask
    return Action(
        intent=Intent.COUNTER,
        facts=facts,
        required={"counter_pct"},
        effects=effects
        + [
            Effect(kind="offer_counter", data=data),
            Effect(kind="set_phase", data={"phase": Phase.NEGOTIATE.value}),
        ],
        next_phase=Phase.NEGOTIATE,
        reason=reason or f"bp={c_next}",
        template_override=template,
    )


def _confirm_required(confirm_facts: dict[str, Fact] | None) -> set[str]:
    """Schedule facts a CONFIRM must speak when the orchestrator supplied them."""
    return set(confirm_facts or {}) & {"offer_total", "num_payments", "first_payment_date"}


# Hold stages: 1 = held once, 2 and 3 = first and second equal step taken.
_LAST_HOLD_STAGE = 3


def _negotiate(t: _Turn, ask_bp: int, afford: Affordability) -> Action:
    """The price ladder (module docstring); every accept is a CONFIRM at a feasible bp.

    Order: ask within our offer → rep's first number → repeat after our final
    counter → firm → ``max_counters`` cap → rep moved (concede half) → hold →
    two equal steps to min(ask, line) → accept at or below the line, else hand off.
    """
    neg, cfg, a = t.neg, t.cfg, t.analysis
    assert afford.max_bp is not None
    line = accept_line_bp(afford.max_bp, cfg.accept_line_pct_of_max_bp)
    legal = sorted(bp for bp in afford.feasible_bps if bp <= line)
    acceptable = ask_bp <= line and ask_bp in afford.feasible_bps

    def confirm(bp: int, reason: str) -> Action:
        return _confirm_action(
            ask_bp=bp,
            belief=t.belief,
            confirm_facts=t.confirm_facts,
            effects=t.effects,
            required=_confirm_required(t.confirm_facts),
            reason=reason,
        )

    def settle(accept_reason: str, over_line_reason: str = "above_accept_line") -> Action:
        if acceptable:
            return confirm(ask_bp, accept_reason)
        reason = over_line_reason if ask_bp > line else "no_legal_counter"
        return _handoff(t.effects, reason)

    def counter(bp: int, stage: int, **kw: Any) -> Action:
        return _counter_action(
            bp,
            t.effects,
            counter_offer_total_cents=t.counter_offer_total_cents,
            ask_bp=ask_bp,
            turn=neg.turn_idx,
            stage=stage,
            **kw,
        )

    prior = list(neg.counters_offered)
    if neg.confirmed_bp is not None:
        prior.append(neg.confirmed_bp)
    # Rep now asks no more than we already offered: take the lower number.
    if prior and ask_bp <= max(prior):
        if acceptable:
            return confirm(ask_bp, "ask_within_offer")
        best = max(prior)
        if best <= line and best in afford.feasible_bps:
            return confirm(best, "ask_within_offer")

    c_prev = neg.counters_offered[-1] if neg.counters_offered else None
    if c_prev is not None and c_prev > line:
        # Revised terms lowered the line under our last offer: never restate it.
        return settle("rep_held")
    if c_prev is not None and t.accepted_term_alt:
        # A yes to a term change reshaped the curve: re-anchor if that moves us up
        # (the old counter can be a token percent the new terms made obsolete).
        fresh = anchor_bp(ask_bp=ask_bp, line_bp=line, legal=legal, anchor_ratio=cfg.anchor_ratio)
        if fresh is not None and fresh > c_prev:
            return counter(fresh, 0)
    if c_prev is None:
        # The rep's first number is never accepted while a lower legal counter exists.
        if a.firm and ask_bp > line:
            return _handoff(t.effects, "above_accept_line")
        anchor = anchor_bp(
            ask_bp=ask_bp, line_bp=line, legal=legal, anchor_ratio=cfg.anchor_ratio
        )
        if anchor is None:
            return settle("no_lower_counter", over_line_reason="no_legal_counter")
        if a.firm:
            final = step_bp(prev=anchor, raw=(anchor + ask_bp) // 2, ceiling=ask_bp, legal=legal)
            return counter(
                final if final is not None else anchor,
                0,
                final_ask=ask_bp,
                reason="final_counter",
                template=_FINAL_COUNTER_TEMPLATE,
            )
        return counter(anchor, 0)

    # They repeated (or bettered) their number after our final counter: accept.
    if neg.final_counter_ask is not None and ask_bp <= neg.final_counter_ask:
        return settle("rep_firm")
    if a.firm:
        if ask_bp > line:
            return _handoff(t.effects, "above_accept_line")
        if len(neg.counter_turns) >= cfg.max_counters:
            return settle("counters_exhausted", over_line_reason="max_counters")
        final = step_bp(prev=c_prev, raw=(c_prev + ask_bp) // 2, ceiling=ask_bp, legal=legal)
        if final is None:
            return settle("rep_firm")
        return counter(
            final, 0, final_ask=ask_bp, reason="final_counter", template=_FINAL_COUNTER_TEMPLATE
        )
    if len(neg.counter_turns) >= cfg.max_counters:
        return settle("counters_exhausted", over_line_reason="max_counters")

    target = min(ask_bp, line)
    moved = neg.ask_at_last_counter is not None and ask_bp < neg.ask_at_last_counter
    if moved:
        assert neg.ask_at_last_counter is not None
        give = int(
            Decimal(str(cfg.concession_factor)) * Decimal(neg.ask_at_last_counter - ask_bp)
        )
        nxt = step_bp(prev=c_prev, raw=c_prev + give, ceiling=min(ask_bp, line + 1), legal=legal)
        if nxt is None or nxt > c_prev + give:
            # Half their move does not reach the next legal bp: restate our offer.
            return counter(c_prev, 0, reason="hold", template=_HOLD_TEMPLATE)
        return counter(nxt, 0)

    stage = neg.hold_stage
    if stage == 0:
        return counter(c_prev, 1, reason="hold", template=_HOLD_TEMPLATE)
    if stage < _LAST_HOLD_STAGE:
        # Two equal steps from the held offer: the first goes half the gap, the
        # second lands on the target itself (highest legal bp at or below it).
        raw = c_prev + max(0, target - c_prev) // 2 if stage == 1 else target
        nxt = step_bp(prev=c_prev, raw=raw, ceiling=target + 1, legal=legal)
        if nxt is not None:
            return counter(nxt, stage + 1, reason="step")
    return settle("rep_held")
