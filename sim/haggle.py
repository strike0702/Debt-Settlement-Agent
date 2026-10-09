"""How the simulated rep haggles over price (Phase 46a).

A ``Haggle`` is a per-scenario knob next to the persona: the persona decides
*what else* the rep does (contradict a rule, pressure for private info), the
haggle style decides how the rep answers a price counter. ``CreditorPolicy``
reads it in ``_on_counter`` / ``_ask_settlement_reply``; ``sim.scenarios``
assigns it in ``generate`` from its own RNG, so the main sampler's draws (and
every scenario's client and rules) stay as they were.

Styles:
- ``easy`` (the pre-46a rep): accepts any counter at or above its floor,
  otherwise drops five points per counter and is firm only at the floor.
- ``holder``: answers ``hold_turns`` counters by restating its number, then
  concedes one step (sizes cycle through ``steps_bp``), and repeats; firm
  at the floor and restates it on every later counter.
- ``stepper``: a holder with no holds and small steps, so the call runs into
  the agent's counter cap.
- ``staller``: never moves. ``stall_on="ask"`` (contradictory persona) will
  not name a number at all, so the agent's repeated-question guard hands off;
  ``"counter"`` (other personas) names one, then answers every counter with no
  number and no stance, so the no-progress guard hands off.

Hidden limits never loosen: every style accepts only at or above the floor,
so ``zopa`` / ``stratum`` labels stay valid. Does not import ``app.agent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Literal

HaggleStyle = Literal["easy", "holder", "stepper", "staller"]
StallOn = Literal["ask", "counter"]

# The pre-46a fixed concession per counter.
EASY_STEP_BP = 500


@dataclass(frozen=True)
class Haggle:
    """Price behaviour of the simulated rep (see module docstring)."""

    style: HaggleStyle = "easy"
    # Counters answered by restating the current number before each concession.
    hold_turns: int = 0
    # Concession sizes in bp, used in order and cycled.
    steps_bp: tuple[int, ...] = (EASY_STEP_BP,)
    stall_on: StallOn | None = None


EASY = Haggle()

# Per-stratum style weights for non-pressuring calls (pressuring calls hand off
# before any price talk). Stallers only in the deal stratum: a stalled no-fix
# call would hand off for a loop-guard reason, which no_deal_correct scores as
# a miss; a stalled deal is a fair test that the agent hands off.
_WEIGHTS: dict[str, tuple[tuple[HaggleStyle, float], ...]] = {
    "deal": (("easy", 0.40), ("holder", 0.25), ("stepper", 0.25), ("staller", 0.10)),
    "no_fix": (("easy", 0.45), ("holder", 0.30), ("stepper", 0.25)),
}


def pick_haggle(rng: Random, *, stratum: str, persona: str) -> Haggle:
    """Draw a style and its knobs from ``rng`` (deterministic for a seeded ``rng``)."""
    weights = _WEIGHTS.get(stratum)
    if weights is None or persona == "pressuring":
        return EASY
    roll = rng.random()
    style: HaggleStyle = weights[-1][0]
    acc = 0.0
    for name, w in weights:
        acc += w
        if roll < acc:
            style = name
            break
    if style == "holder":
        return Haggle(
            style="holder",
            hold_turns=rng.choice((1, 2)),
            steps_bp=tuple(rng.choice((300, 400, 500, 700, 900)) for _ in range(3)),
        )
    if style == "stepper":
        return Haggle(
            style="stepper",
            steps_bp=tuple(rng.choice((200, 300, 400)) for _ in range(3)),
        )
    if style == "staller":
        # Same draw as before; the persona picks the stall so both loop guards
        # (repeated question, no progress) show up whenever stallers do.
        rng.random()
        stall_on: StallOn = "ask" if persona == "contradictory" else "counter"
        return Haggle(style="staller", stall_on=stall_on, steps_bp=())
    return EASY
