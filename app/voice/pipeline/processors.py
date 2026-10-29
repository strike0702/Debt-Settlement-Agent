"""Pipecat frame processors of the call pipeline (ours; Pipecat supplies the rest).

Pipeline order (``app.voice.pipeline.session``)::

    transport.input → SilenceFill → RepVad → DeepgramFluxSTTService
      → OrchestratorProcessor → DeepgramTTSService → Tap("tts") → OutputGate
      → transport.output → Tap("out")

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

Barge-in is two-step (Phase 65, user decision 2026-10-29). ``RepVad`` (Silero,
local) hears the rep start talking over the agent and pauses its audio at
``OutputGate`` within a few hundred ms; Flux's words then decide: a real barge
(``rules.is_barge``) stops and drops the rest, a backchannel or nothing resumes
where it paused. Waiting for words alone stopped the agent about 1.4 s late.
``OutputGate`` releases audio only ``LEAD_S`` ahead of real time so a pause
takes effect at once, and trims the leading silence Aura-2 puts before the
first sentence of a reply (about 0.25 s of the reply latency).

The TTS service keeps data frames in order behind the audio before them, and
the output transport pushes a data frame on only after the audio before it was
written, so ``Tap("out")`` sees ``SentenceEndFrame``
when that sentence finished playing: that is "heard" (``HeardTracker``).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipecat.audio.utils import detect_speech_onset
from pipecat.audio.vad.vad_analyzer import VADState
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
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.voice.pipeline.heard import HeardTracker
from app.voice.pipeline.rules import is_backchannel, is_barge
from app.voice.pipeline.turns import VoiceTurns

# Longest gap in input audio before zeros are sent to Flux, and the fill chunk.
GAP_S = 0.06
CHUNK_S = 0.02
# Agent audio released ahead of real time (how late a pause can take effect).
LEAD_S = 0.08
# Leading silence trimmed from the first sentence of a reply: at most this much.
TRIM_MAX_S = 0.3
# A reply not started this long after the rep's final gets a filler first.
# Measured (docs/eval/voice_tuned_20261029): LLM turns reply 1.5 to 2.9 s after
# the final; a filler at 1.5 s made most of them wait behind it, so only a
# reply later than that p95 gets one. Fast-path replies take under 50 ms.
FILLER_AFTER_S = 3.0
# Spoken while a late reply is computed; never acked, never part of a turn.
FILLERS = ("One moment.", "Let me check.", "Okay, just a second.", "Let me see.")
# A pause with no barge resumes this long after the rep's speech stops, and
# never lasts longer than PAUSE_MAX_S.
RESUME_AFTER_S = 0.6
PAUSE_MAX_S = 2.5

Log = Callable[..., None]


def _no_log(kind: str, **data: Any) -> None:
    return None


@dataclass
class SentenceEndFrame(DataFrame):
    """Marks the end of one agent sentence's audio in the frame stream."""

    sid: str


@dataclass
class ReplyStartFrame(DataFrame):
    """Marks where a reply's audio starts (after any filler), for the timeline."""


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


class OutputGate(FrameProcessor):
    """Paces agent audio ``LEAD_S`` ahead of playback so it can pause and resume.

    Frames keep their order; non-audio frames (sentence ends, TTS markers) go
    out as soon as the audio before them has. An interruption drops what is
    held. While nothing is playing, a sentence's leading silence is trimmed.
    """

    def __init__(self, *, log: Log = _no_log) -> None:
        super().__init__()
        self.log = log
        self.paused = False
        self._held: deque[Frame] = deque()
        self._until = 0.0  # monotonic time the released audio runs out
        self._trim = False
        self._trim_buf: list[OutputAudioRawFrame] = []
        self._trim_side: list[Frame] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def holding(self) -> bool:
        """Agent audio is held (paused or waiting for its turn to play)."""
        return any(isinstance(f, OutputAudioRawFrame) for f in self._held)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        self._wake.set()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            if self._task is None:
                self._task = self.create_task(self._release())
            return
        if isinstance(frame, (InterruptionFrame, CancelFrame)):
            self._held.clear()
            self._trim_buf, self._trim_side, self._trim = [], [], False
            self._until = 0.0
            self.paused = False
            if isinstance(frame, CancelFrame) and self._task is not None:
                await self.cancel_task(self._task)
                self._task = None
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, EndFrame):
            self.paused = False  # the end drains whatever is held
        quiet = time.monotonic() >= self._until and not self.holding
        if isinstance(frame, TTSStartedFrame) and quiet:
            self._trim = True  # first sentence after silence: drop Aura-2's lead-in
        if isinstance(frame, OutputAudioRawFrame) and self._trim:
            self._trimmed(frame)
            return
        if self._trim_buf and not isinstance(frame, (SentenceEndFrame, TTSStoppedFrame, EndFrame)):
            self._trim_side.append(frame)  # word timings etc.: after the trimmed audio
            return
        if self._trim_buf:
            self._flush_trim()  # never hold an end marker ahead of its audio
        self._held.append(frame)
        self._wake.set()

    def _trimmed(self, frame: OutputAudioRawFrame) -> None:
        """Buffer a reply's first audio until speech onset; drop what comes before it.

        Onset is Pipecat's energy test (``detect_speech_onset``, the one its TTFA
        metric uses); at most ``TRIM_MAX_S`` is dropped. The trimmed audio, and
        any frames that came with it, go to the queue here.
        """
        self._trim_buf.append(frame)
        audio = b"".join(f.audio for f in self._trim_buf)
        width = 2 * frame.num_channels
        budget = int(TRIM_MAX_S * frame.sample_rate) * width
        onset = detect_speech_onset(audio, frame.sample_rate, frame.num_channels)
        if onset is None and len(audio) < budget + frame.sample_rate // 10 * width:
            return
        block = frame.sample_rate // 100 * width
        cut = min(budget, (onset or 0) * width) // block * block
        first = self._trim_buf[0]
        self._trim_buf = []
        self._trim = False
        self.log("trim", ms=round(cut / width / frame.sample_rate * 1000))
        out = type(first)(audio[cut:], frame.sample_rate, frame.num_channels)
        if isinstance(first, TTSAudioRawFrame) and isinstance(out, TTSAudioRawFrame):
            out.context_id = first.context_id  # the output keys bot speech on it
        self._held.append(out)
        self._held.extend(self._trim_side)
        self._trim_side = []
        self._wake.set()

    def _flush_trim(self) -> None:
        """A short sentence ended before onset was confirmed: play it untrimmed."""
        self._held.extend(self._trim_buf)
        self._held.extend(self._trim_side)
        self._trim_buf, self._trim_side, self._trim = [], [], False

    async def _release(self) -> None:
        while True:
            if not self._held or self.paused:
                self._wake.clear()
                await self._wake.wait()
                continue
            frame = self._held[0]
            if isinstance(frame, OutputAudioRawFrame):
                now = time.monotonic()
                if self._until - now > LEAD_S:
                    await asyncio.sleep(min(0.01, self._until - now - LEAD_S))
                    continue
                n = len(frame.audio) / (2 * frame.num_channels)
                self._until = max(now, self._until) + n / frame.sample_rate
            self._held.popleft()
            await self.push_frame(frame)


class RepVad(FrameProcessor):
    """Local voice activity on the rep's audio; reports speech starts and stops.

    Runs Silero on every input frame (cheap on CPU); the owner decides what a
    start means (only while the agent talks: pause it).
    """

    def __init__(self, analyzer: Any, on_change: Callable[[bool], Any]) -> None:
        super().__init__()
        self.analyzer = analyzer
        self.on_change = on_change
        self._speaking = False
        self._rate = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if not isinstance(frame, InputAudioRawFrame):
            return
        if frame.sample_rate != self._rate:
            self._rate = frame.sample_rate
            self.analyzer.set_sample_rate(frame.sample_rate)
        state = await self.analyzer.analyze_audio(frame.audio)
        speaking = state in (VADState.SPEAKING, VADState.STOPPING)
        if speaking != self._speaking:
            self._speaking = speaking
            await self.on_change(speaking)


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
        # Set by the session when VAD pausing is on (``OutputGate``).
        self.gate: OutputGate | None = None
        self._resume_task: asyncio.Task[None] | None = None
        self.filler_after_s: float | None = FILLER_AFTER_S
        self._filler_task: asyncio.Task[None] | None = None
        self._fillers = 0

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

    async def on_rep_voice(self, speaking: bool) -> None:
        """``RepVad``: the rep started / stopped talking. Over the agent: pause it."""
        gate = self.gate
        if gate is None:
            return
        if speaking and self.agent_talking and not gate.paused:
            gate.pause()
            self.log("pause")
            self._resume_later(PAUSE_MAX_S, "max")
        elif not speaking and gate.paused:
            self._resume_later(RESUME_AFTER_S, "quiet")

    def _resume_later(self, delay: float, reason: str) -> None:
        if self._resume_task is not None:
            self._resume_task.cancel()
        self._cancel_filler()

        async def later() -> None:
            await asyncio.sleep(delay)
            self._resume(reason)

        self._resume_task = asyncio.get_running_loop().create_task(later())

    def _resume(self, reason: str) -> None:
        """No barge after a pause (backchannel, noise or echo): play on."""
        if self._resume_task is not None and reason in ("backchannel", "barge"):
            self._resume_task.cancel()
        if self.gate is not None and self.gate.paused:
            self.gate.resume()
            if reason != "barge":
                self.log("resume", reason=reason)

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
                self._resume("backchannel")
                return
            await self.interrupt(reason="final", text=text)
        self.log("final", text=text)
        self._show("thinking")
        self._spawn(self.turns.on_final(text))
        if self.filler_after_s is not None:
            self._cancel_filler()
            self._filler_task = asyncio.get_running_loop().create_task(self._filler())

    async def _filler(self) -> None:
        """Say a short filler if the reply is still not out after ``filler_after_s``."""
        assert self.filler_after_s is not None
        await asyncio.sleep(self.filler_after_s)
        if self.agent_talking:
            return
        text = FILLERS[self._fillers % len(FILLERS)]
        self._fillers += 1
        self.tts_chars += len(text)
        self.log("filler", text=text)
        await self.push_frame(TTSSpeakFrame(text))

    def _cancel_filler(self) -> None:
        if self._filler_task is not None:
            self._filler_task.cancel()
            self._filler_task = None

    async def interrupt(self, *, reason: str, text: str = "", barge: bool = True) -> None:
        """Stop the agent's audio; with ``barge`` tell the orchestrator what was heard."""
        split = self.tracker.interrupt()
        self._barged = True
        self._resume("barge")  # the interruption below drops what the gate held
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
        self._cancel_filler()
        self._barged = False
        if sentences:
            self.log(
                "reply",
                sids=[sid for sid, _ in sentences],
                text=sentences[0][1],
                chars=sum(len(t) for _, t in sentences),
            )
        if sentences:
            await self.push_frame(ReplyStartFrame())
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
        if self._resume_task is not None:
            self._resume_task.cancel()
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
        self._reply_audio = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self._first_byte = True
        elif isinstance(frame, TTSAudioRawFrame) and self._first_byte and self.where == "tts":
            self._first_byte = False
            self.log("tts_byte")
        elif isinstance(frame, ReplyStartFrame):
            self._reply_audio = True
        elif isinstance(frame, OutputAudioRawFrame) and self._reply_audio and self.where == "out":
            self._reply_audio = False
            self.log("reply_audio")
        elif isinstance(frame, SentenceEndFrame) and self.on_played is not None:
            self.on_played(frame.sid)
        elif isinstance(frame, BotStartedSpeakingFrame) and self.where == "out":
            self.log("bot_started")
        elif isinstance(frame, BotStoppedSpeakingFrame) and self.where == "out":
            self.log("bot_stopped")
        await self.push_frame(frame, direction)
