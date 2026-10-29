"""Voice turns → the call's orchestrator, the same path the WebSocket ``text`` event takes.

``VoiceTurns`` sits between the Pipecat processors and the call
(``VoiceCall``, implemented by ``app.voice.ws``'s connection). A final Flux
transcript runs ``Orchestrator.on_creditor_text`` and the call emits the reply
as usual (UI events on the socket, sentences to TTS). Played sentences are
acked with ``on_sentence_done``; a barge passes the played ids to
``on_barge_in``, so nothing-heard rollback and fragment merge apply unchanged.

Eager end of turn: Flux's ``EagerEndOfTurn`` may start the turn early, without
emitting it. When the committed transcript says the same words, that turn is
emitted as is. When it differs, or Flux reported ``TurnResumed``, the early turn
is withdrawn (``Orchestrator.withdraw_unheard_turn``: rolled back, nothing was
spoken) and the committed text runs as a fresh turn. No policy here.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol

from app.agent.orchestrator import Orchestrator, Utterance
from app.llm.client import LLMUnavailable
from app.voice.pipeline.rules import words


class VoiceCall(Protocol):
    """What a voice session needs from the call it joined (see ``app.voice.ws``)."""

    orch: Orchestrator | None

    async def show_creditor(self, text: str) -> None:
        """Send the rep's line to the UI (``transcript``, role creditor)."""

    async def emit(self, utt: Utterance) -> None:
        """Emit a reply: UI events, and its sentences to the voice session."""

    async def ack(self, ids: list[str]) -> None:
        """``on_sentence_done`` plus the UI events the socket sends after an ack."""

    async def barge(self, spoken: list[str]) -> None:
        """``on_barge_in`` plus the UI events the socket sends after a barge."""

    async def nlu_unavailable(self, e: LLMUnavailable) -> None:
        """No NLU provider answered: tell the UI and keep the call alive."""

    def audit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append a ``voice`` row to the call's audit log."""

    async def voice_state(self, state: str, text: str) -> None:
        """Show the browser what the call is doing (``voice_state``; display only)."""


class VoiceTurns:
    """Runs final / eager transcripts and heard / barge signals against one call."""

    def __init__(self, call: VoiceCall, *, eager: bool) -> None:
        self.call = call
        self.eager = eager
        self._early: tuple[str, asyncio.Task[Utterance]] | None = None
        self._resumed = False

    def on_eager(self, text: str) -> None:
        """Start the turn on Flux's likely-final transcript (held, not emitted)."""
        orch = self.call.orch
        if not self.eager or orch is None or self._early is not None or not text.strip():
            return
        self._resumed = False
        self._early = (text, asyncio.create_task(orch.on_creditor_text(text)))
        self.call.audit("eager_turn", {"text": text[:200]})

    def on_resumed(self) -> None:
        """The rep kept talking: the early turn will not be used."""
        if self._early is not None:
            self._resumed = True

    async def on_final(self, text: str) -> None:
        """Run the committed turn and emit its reply."""
        orch = self.call.orch
        if orch is None or not text.strip():
            return
        await self.call.show_creditor(text)
        early, self._early = self._early, None
        try:
            utt = await self._turn(orch, text, early)
        except LLMUnavailable as e:
            await self.call.nlu_unavailable(e)
            return
        await self.call.emit(utt)

    async def _turn(
        self, orch: Orchestrator, text: str, early: tuple[str, asyncio.Task[Utterance]] | None
    ) -> Utterance:
        if early is None:
            return await orch.on_creditor_text(text)
        early_text, task = early
        same = not self._resumed and words(early_text) == words(text)
        if same:
            with contextlib.suppress(LLMUnavailable):
                return await task
        else:
            with contextlib.suppress(LLMUnavailable):
                await task
            await orch.withdraw_unheard_turn()
        self.call.audit("eager_withdrawn", {"early": early_text[:200], "final": text[:200]})
        return await orch.on_creditor_text(text)

    async def on_played(self, sid: str) -> None:
        """A sentence's audio finished playing: ack it."""
        await self.call.ack([sid])

    async def on_barge(self, heard: list[str]) -> None:
        """The rep barged in: keep what was fully played, drop the rest."""
        await self.call.barge(heard)

    async def aclose(self) -> None:
        """Drop an early turn still running when the session ends."""
        early, self._early = self._early, None
        if early is not None:
            early[1].cancel()
            await asyncio.gather(early[1], return_exceptions=True)
