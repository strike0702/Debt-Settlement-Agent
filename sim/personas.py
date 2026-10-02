"""Creditor sim personas: style flags and turn-script hooks.

Personas do not decide accept/reject math — that is ``CreditorPolicy``. They
only control when to contradict a revealed rule and when to pressure
(private-info / commitment asks). MVP: flexible, contradictory, pressuring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PersonaName = Literal["flexible", "contradictory", "pressuring"]

PERSONAS: tuple[PersonaName, ...] = ("flexible", "contradictory", "pressuring")


@dataclass(frozen=True)
class Persona:
    """Behavioral knobs for the code-based creditor simulator."""

    name: PersonaName
    # contradictory: flip one revealed numeric rule once.
    contradict_once: bool = False
    # pressuring: private ask on creditor-turns 2 and 3; commit on turn 3.
    pressure_private_turns: tuple[int, ...] = ()
    pressure_commit_turns: tuple[int, ...] = ()


PERSONA_BY_NAME: dict[PersonaName, Persona] = {
    "flexible": Persona(name="flexible"),
    "contradictory": Persona(name="contradictory", contradict_once=True),
    "pressuring": Persona(
        name="pressuring",
        # Early turns so short no_fix/rescue calls still escalate.
        pressure_private_turns=(2, 3),
        pressure_commit_turns=(3,),
    ),
}


def get_persona(name: PersonaName | str) -> Persona:
    """Look up a persona by name; unknown names raise ``KeyError``."""
    return PERSONA_BY_NAME[name]  # type: ignore[index]
