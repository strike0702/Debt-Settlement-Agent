"""Pipecat frame processors of the call pipeline (ours; Pipecat supplies the rest).

Pipeline order (``app.voice.pipeline.session``)::

    transport.input → SilenceFill → DeepgramFluxSTTService → OrchestratorProcessor
      → DeepgramTTSService → Tap("tts") → transport.output → Tap("out")

``SilenceFill`` keeps audio flowing into Flux. The WebRTC input delivers no
frames while the browser sends silence, and Pipecat's Flux watchdog only fills
gaps after a start of turn, so a lone "Correct." got no trailing audio, never
started a turn and was glued to the rep's next line (Phase 62). Zeros fill any
gap longer than ``GAP_S``.

``OrchestratorProcessor`` takes Flux's final transcripts to ``VoiceTurns``,
decides barge-ins itself (Flux runs with ``should_interrupt=False``; see
``rules``), and speaks each reply sentence as one ``TTSSpeakFrame`` followed by
a ``SentenceEndFrame``. It also tells the browser what the call is doing
(``VoiceCall.voice_state``: live partials, thinking, agent audio on / off) so
the WebRTC client can show it; that is display only, never a turn.

The TTS service keeps data frames in order behind the audio before them, and
the output transport pushes a data frame on only after the audio before it was
written, so ``Tap("out")`` sees ``SentenceEndFrame``
when that sentence finished playing: that is "heard" (``HeardTracker``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    DataFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.voice.pipeline.heard import HeardTracker
from app.voice.pipeline.rules import is_backchannel, is_barge
from app.voice.pipeline.turns import VoiceTurns

# Longest gap in input audio before zeros are sent to Flux, and the fill chunk.
GAP_S = 0.06
CHUNK_S = 0.02

Log = Callable[..., None]


def _no_log(kind: str, **data: Any) -> None:
    return None


@dataclass
class SentenceEndFrame(DataFrame):
    """Marks the end of one agent sentence's audio in the frame stream."""

    sid: str


class SilenceFill(FrameProcessor):
    """Sends zero audio downstream whenever input audio pauses for more than ``GAP_S``."""

    def __init__(self) -> None:
        super().__init__()
        self._last = 0.0
        self._rate = 0
        self._channels = 1
        self._task: asyncio.Task[None] | None = None
        self.filled_s = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._last = time.monotonic()
            self._rate, self._channels = frame.sample_rate, frame.num_channels
        elif isinstance(frame, StartFrame) and self._task is None:
            self._task = self.create_task(self._fill())
        elif isinstance(frame, (EndFrame, CancelFrame)) and self._task is not None:
            await self.cancel_task(self._task)
            self._task = None
        await self.push_frame(frame, direction)

    async def _fill(self) -> None:
        while True:
            await asyncio.sleep(CHUNK_S)
            if self._rate and time.monotonic() - self._last > GAP_S:
                n = int(self._rate * CHUNK_S)
                silence = b"\x00\x00" * n * self._channels
                await self.push_frame(InputAudioRawFrame(silence, self._rate, self._channels))
                self.filled_s += CHUNK_S


class OrchestratorProcessor(FrameProcessor):
    """Final transcripts → ``VoiceTurns``; reply sentences → TTS; barge decisions."""

    def __init__(self, turns: VoiceTurns, tracker: HeardTracker, *, log: Log = _no_log) -> None:
        super().__init__()
        self.turns = turns
        self.tracker = tracker
        self.log = log
        self.bot_speaking = False
        # Set by a barge until the next reply, so one barge is not counted twice
        # while the output transport is still stopping.
        self._barged = False
        self.tts_chars = 0
        self._spawned: set[asyncio.Task[None]] = set()
        self.on_tts_request: Callable[[str, str], None] | None = None
        self._shown: tuple[str, str] | None = None

    @property
    def agent_talking(self) -> bool:
        """The agent is playing audio or has sentences waiting to play."""
        return (self.bot_speaking or self.tracker.speaking) and not self._barged

    def _show(self, state: str, text: str = "") -> None:
        """Send the browser's call state once per change (``voice_state``)."""
        if self._shown == (state, text):
            return
        self._shown = (state, text)
        self._spawn(self.turns.call.voice_state(state, text))

    def _spawn(self, coro: Any) -> None:
        # Turns run beside the pipeline so frames keep flowing while NLU runs.
        task = asyncio.get_running_loop().create_task(coro)
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            await self._final(frame.text)
            return
        if isinstance(frame, InterimTranscriptionFrame):
            self._show("rep_speaking", frame.text.strip())
            if self.agent_talking and is_barge(frame.text):
                await self.interrupt(reason="interim", text=frame.text)
            return
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            self._show("agent_speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            self._show("listening")
        await self.push_frame(frame, direction)

    async def _final(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.agent_talking:
            if is_backchannel(text):
                # "Okay." over the agent: not a turn and not a barge.
                self.log("backchannel", text=text)
                self.turns.call.audit("backchannel_ignored", {"text": text[:80]})
                return
            await self.interrupt(reason="final", text=text)
        self.log("final", text=text)
        self._show("thinking")
        self._spawn(self.turns.on_final(text))

    async def interrupt(self, *, reason: str, text: str = "", barge: bool = True) -> None:
        """Stop the agent's audio; with ``barge`` tell the orchestrator what was heard."""
        split = self.tracker.interrupt()
        self._barged = True
        # Pushed by hand, not broadcast_interruption(): that resets this
        # processor's own task, which may be the one running this handler.
        await self.push_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await self.push_frame(InterruptionFrame(), FrameDirection.UPSTREAM)
        self.log("barge", reason=reason, text=text, heard=split.heard, dropped=split.dropped)
        if barge:
            self.turns.call.audit(
                "voice_barge",
                {"reason": reason, "text": text[:80], "heard": split.heard,
                 "dropped": split.dropped},
            )  # fmt: skip
            self._spawn(self.turns.on_barge(split.heard))

    async def speak(self, sentences: list[tuple[str, str]]) -> None:
        """Queue reply sentences for TTS, each followed by its end marker."""
        self._barged = False
        if sentences:
            self.log(
                "reply",
                sids=[sid for sid, _ in sentences],
                text=sentences[0][1],
                chars=sum(len(t) for _, t in sentences),
            )
        for sid, text in sentences:
            self.tracker.queued(sid)
            self.tts_chars += len(text)
            if self.on_tts_request is not None:
                self.on_tts_request(sid, text)
            await self.push_frame(TTSSpeakFrame(text))
            await self.push_frame(SentenceEndFrame(sid))

    def played(self, sid: str) -> None:
        """``Tap("out")`` saw this sentence's end marker: it was heard; ack it."""
        if self.tracker.played(sid):
            self.log("heard", sid=sid)
            self._spawn(self.turns.on_played(sid))

    async def drop(self, ids: list[str]) -> None:
        """The orchestrator dropped these sentences: stop them if they are still queued."""
        if set(ids) & set(self.tracker.outstanding()):
            await self.interrupt(reason="dropped", barge=False)

    async def aclose(self) -> None:
        """Wait for turns still running (cancelled at session end)."""
        for task in list(self._spawned):
            task.cancel()
        await asyncio.gather(*self._spawned, return_exceptions=True)


class Tap(FrameProcessor):
    """Passes every frame on; logs timing and reports played sentences (after output)."""

    def __init__(
        self,
        where: str,
        *,
        log: Log = _no_log,
        on_played: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.where = where
        self.log = log
        self.on_played = on_played
        self._first_byte = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self._first_byte = True
        elif isinstance(frame, TTSAudioRawFrame) and self._first_byte and self.where == "tts":
            self._first_byte = False
            self.log("tts_byte")
        elif isinstance(frame, SentenceEndFrame) and self.on_played is not None:
            self.on_played(frame.sid)
        elif isinstance(frame, BotStartedSpeakingFrame) and self.where == "out":
            self.log("bot_started")
        elif isinstance(frame, BotStoppedSpeakingFrame) and self.where == "out":
            self.log("bot_stopped")
        await self.push_frame(frame, direction)
