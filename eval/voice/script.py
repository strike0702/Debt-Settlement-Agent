"""Scripted rep lines for the voice harness: what is said, and when.

Each ``CallScript`` is a curated scenario plus an ordered list of rep lines.
A line's ``kind`` says when the driver plays it:

- ``reply``: after the agent has finished its turn (normal turn taking);
- ``backchannel``: ``offset_ms`` after the agent starts speaking; the agent
  should keep talking (any barge-in here is a false barge-in);
- ``interrupt``: ``offset_ms`` after the agent starts speaking; a real
  interruption that should stop the agent quickly.

A line with ``parts`` is rendered as those parts joined by ``pause_ms`` of
silence (the mid-sentence pause case). The WAVs live in ``fixtures/voice/``,
made once by ``eval.voice.fixtures``. Lines are synthetic and digit-free: the
expected numbers are read from the words by ``eval.voice.metrics``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

LineKind = Literal["reply", "backchannel", "interrupt"]


@dataclass(frozen=True)
class RepLine:
    """One rep utterance; ``text`` is the reference transcript."""

    id: str
    text: str
    kind: LineKind = "reply"
    parts: tuple[str, ...] = ()
    pause_ms: int = 0
    offset_ms: int = 0

    @property
    def wav_name(self) -> str:
        return f"{self.id}.wav"


@dataclass(frozen=True)
class CallScript:
    """A curated scenario id and the rep lines played into it, in order."""

    id: str
    scenario_id: str
    lines: tuple[RepLine, ...] = field(default_factory=tuple)


def _pause(id: str, first: str, second: str, pause_ms: int = 1300) -> RepLine:
    return RepLine(id, f"{first} {second}", parts=(first, second), pause_ms=pause_ms)


CALLS: tuple[CallScript, ...] = (
    CallScript(
        "numbers_easy",
        "easy_deal",
        (
            RepLine("a01_mmhm", "Mm-hmm.", kind="backchannel", offset_ms=2500),
            RepLine(
                "a02_terms",
                "Sure. We can take up to eight monthly payments, "
                "at least one hundred twenty dollars each, all the same amount.",
            ),
            RepLine("a03_correct", "Correct."),
            _pause("a04_pause", "We are looking for", "forty-five percent of the balance."),
            RepLine(
                "a05_interrupt",
                "Hold on, hold on. That is too low. I could do forty-two percent.",
                kind="interrupt",
                offset_ms=1200,
            ),
            RepLine("a06_date", "The first payment would be on November fifteenth."),
            RepLine("a07_yes", "Yes, that works for us."),
            RepLine("a08_bye", "No, that's everything. Thanks."),
        ),
    ),
    CallScript(
        "numbers_haggle",
        "haggling_rep",
        (
            RepLine("b01_okay", "Okay.", kind="backchannel", offset_ms=3000),
            RepLine(
                "b02_terms",
                "We can take up to eight monthly payments, "
                "at least one hundred dollars each, all the same amount.",
            ),
            RepLine("b03_correct", "Correct."),
            RepLine("b04_seventy", "We're looking for seventy percent of the balance."),
            RepLine(
                "b05_interrupt",
                "Wait, wait. We can't move on that. Still seventy percent.",
                kind="interrupt",
                offset_ms=1200,
            ),
            _pause("b06_pause", "Sixty-five percent is", "the lowest we can go.", 1200),
            RepLine("b07_right", "Right.", kind="backchannel", offset_ms=1500),
            RepLine("b08_date", "Can the first payment be on December first, twenty twenty-six?"),
            RepLine("b09_yes", "Yes, that works for us."),
            RepLine("b10_bye", "No, that's everything. Thanks."),
        ),
    ),
)


def all_lines() -> list[RepLine]:
    """Every line of every call (fixture generation)."""
    return [line for call in CALLS for line in call.lines]


def call_by_id(call_id: str) -> CallScript:
    """The script with ``call_id``; ``KeyError`` if unknown."""
    for call in CALLS:
        if call.id == call_id:
            return call
    raise KeyError(call_id)
