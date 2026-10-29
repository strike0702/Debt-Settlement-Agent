"""Which agent sentences the rep actually heard, from the output transport's playback.

``HeardTracker`` is plain bookkeeping, no Pipecat: the session tells it a
sentence was queued for TTS (``queued``), a playout probe after the output
transport tells it the sentence's audio finished (``played``), and an
interruption asks it for the split (``interrupt``). A sentence counts as heard
only once its last audio frame was written to the WebRTC track; a sentence cut
mid-way is not heard. The caller acks played sentences with
``Orchestrator.on_sentence_done`` and passes ``interrupt().heard`` to
``on_barge_in``, so the Phase 56 rollback (nothing heard) and fragment merge
apply unchanged. No browser acks on this path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BargeSplit:
    """Sentences of the interrupted utterance: fully played vs cut or never played."""

    heard: list[str]
    dropped: list[str]


class HeardTracker:
    """Queued → played sentence ids for the utterance being spoken."""

    def __init__(self) -> None:
        self._queued: list[str] = []
        self._played: list[str] = []

    @property
    def speaking(self) -> bool:
        """True while some queued sentence has not finished playing."""
        return len(self._played) < len(self._queued)

    def outstanding(self) -> list[str]:
        """Queued sentence ids not yet played, in order."""
        return [sid for sid in self._queued if sid not in self._played]

    def queued(self, sid: str) -> None:
        """A sentence was sent to TTS."""
        if not self.speaking:
            # Everything before was played: a new utterance starts.
            self._queued, self._played = [], []
        self._queued.append(sid)

    def played(self, sid: str) -> bool:
        """Its audio finished on the output; False for an id this tracker does not hold."""
        if sid not in self._queued or sid in self._played:
            return False
        self._played.append(sid)
        return True

    def interrupt(self) -> BargeSplit:
        """Split the current utterance and forget it (its unplayed audio is flushed)."""
        split = BargeSplit(heard=list(self._played), dropped=self.outstanding())
        self._queued, self._played = [], []
        return split
