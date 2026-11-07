"""Phase 70: instant ack rules and the number-free sentence audio cache.

No Deepgram: the TTS service runs with its websocket stubbed and a fake cache,
so a hit's frames go through Pipecat's real audio-context path.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.tests.utils import run_test

from app.voice.pipeline import processors
from app.voice.pipeline.heard import HeardTracker
from app.voice.pipeline.processors import OrchestratorProcessor, SentenceEndFrame
from app.voice.pipeline.tts_cache import CachedDeepgramTTS, SentenceAudioCache, cacheable
from tests.unit.test_voice_pipeline import FakeTurns


class Turns(FakeTurns):
    """FakeTurns that also records heard sentences."""

    def __init__(self) -> None:
        super().__init__()
        self.heard: list[str] = []

    async def on_played(self, sid: str) -> None:
        self.heard.append(sid)


class SlowTurns(Turns):
    """A turn that stays in NLU until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def on_final(self, text: str) -> None:
        self.finals.append(text)
        await self.release.wait()


async def _proc(turns: Any = None) -> tuple[OrchestratorProcessor, list[Any], list[str]]:
    """A processor past its opening, with ack on (20 ms) and pushes and log captured."""
    proc = OrchestratorProcessor(turns or FakeTurns(), HeardTracker())
    proc.filler_after_s = None
    proc.ack_after_s = 0.02
    sent: list[Any] = []
    logged: list[str] = []

    async def push(frame: Any, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        sent.append(frame)

    proc.push_frame = push  # type: ignore[method-assign]
    proc.log = lambda kind, **d: logged.append(kind)
    await proc.speak([("o1", "Hello, this is the agent.")])
    proc.tracker.played("o1")
    return proc, sent, logged


def _spoken(sent: list[Any]) -> list[str]:
    return [f.text for f in sent if isinstance(f, TTSSpeakFrame)]


# ---------------------------------------------------------------- instant ack


async def test_ack_once_on_a_model_turn_never_on_a_fast_path() -> None:
    turns = SlowTurns()
    proc, sent, logged = await _proc(turns)
    await proc._final("We can do eight payments.")
    await asyncio.sleep(0.05)  # no reply yet: the model has it
    await proc._final("Monthly.")  # more words for the same turn: no second ack
    await asyncio.sleep(0.05)
    assert _spoken(sent)[1:] == [processors.ACKS[0]] and logged.count("ack") == 1
    turns.release.set()
    await proc.speak([("r1", "Thanks. What is the minimum?")])
    await asyncio.sleep(0.01)
    # A fast path replies within the delay: no ack.
    proc.tracker.played("r1")
    proc._ack_only = False
    await proc._final("Correct.")
    await proc.speak([("r2", "Got it.")])
    await asyncio.sleep(0.05)
    assert logged.count("ack") == 1
    await proc.aclose()


async def test_no_ack_before_the_opening_after_the_end_or_over_agent_audio() -> None:
    proc = OrchestratorProcessor(FakeTurns(), HeardTracker())  # type: ignore[arg-type]
    proc.ack_after_s, proc.filler_after_s = 0.01, None
    sent: list[Any] = []

    async def push(frame: Any, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        sent.append(frame)

    proc.push_frame = push  # type: ignore[method-assign]
    await proc._final("Hello?")  # before the opening was queued
    await asyncio.sleep(0.03)
    assert sent == []
    proc, sent, _ = await _proc()
    proc.call_over = lambda: True  # the call's last line is out
    await proc._final("Bye.")
    await asyncio.sleep(0.03)
    assert _spoken(sent)[1:] == []
    proc, sent, _ = await _proc()
    proc.gate = processors.OutputGate()
    proc.gate.pause()  # the agent is paused
    await proc._final("Hmm, let me think about that.")
    await asyncio.sleep(0.03)
    assert _spoken(sent)[1:] == []
    proc, sent, _ = await _proc()
    proc.tracker.queued("r1")
    proc.bot_speaking = True
    await proc._final("Hold on, that is too low.")  # a barge: the rep goes on talking
    proc.bot_speaking = False
    await asyncio.sleep(0.03)
    assert _spoken(sent)[1:] == []
    await proc.aclose()


async def test_ack_audio_is_not_agent_talk_for_barge_or_pause() -> None:
    turns = FakeTurns()
    proc, sent, _ = await _proc(turns)
    proc.gate = processors.OutputGate()
    await proc._final("We need seventy percent.")
    await asyncio.sleep(0.05)
    proc.bot_speaking = True  # the ack is playing
    assert not proc.agent_talking
    await proc.on_rep_voice(True)
    assert not proc.gate.paused  # never paused or cut
    await proc._final("No, wait, I meant sixty five.")
    await asyncio.sleep(0.01)
    assert turns.barges == [] and not any(isinstance(f, InterruptionFrame) for f in sent)
    assert turns.finals[-1] == "No, wait, I meant sixty five."
    await proc.aclose()


# ---------------------------------------------------------------- sentence cache


def test_only_number_free_sentences_are_cacheable(tmp_path: Path) -> None:
    assert cacheable("What is the minimum monthly payment?")
    assert cacheable("Mm-hm.") and cacheable("Right.")
    for text in (
        "Can you do 45%?",
        "Is that $120?",
        "Is it forty-five percent?",
        "The first payment would be on the fifteenth.",
        "One moment.",
        "Could you do a hundred?",
        "Just a second.",
    ):
        assert not cacheable(text), text
    cache = SentenceAudioCache(tmp_path, max_bytes=100)
    cache.put("k1", "Is it twenty percent?", b"\x01" * 10)
    assert cache.get("k1") is None  # refused at the store too
    cache.put("a", "Okay then.", b"\x01" * 40)
    cache.put("b", "Thank you.", b"\x02" * 40)
    assert cache.get("a") is not None  # now the most recently used
    cache.put("c", "Goodbye now.", b"\x03" * 40)  # over 100 bytes: evict the oldest
    assert cache.get("b") is None and cache.get("a") and cache.get("c")
    assert SentenceAudioCache(tmp_path, max_bytes=100).size == 80  # reloads from disk


class FakeCache:
    """Every lookup hits; records what is stored."""

    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.gets: list[str] = []
        self.puts: list[tuple[str, bytes]] = []

    def get(self, key: str) -> bytes | None:
        self.gets.append(key)
        return self.audio

    def put(self, key: str, text: str, audio: bytes) -> None:
        if cacheable(text):
            self.puts.append((text, audio))


def _tts(cache: Any, synth: list[tuple[str, bool]], monkeypatch: Any) -> CachedDeepgramTTS:
    async def nothing(self: Any) -> None:
        return None

    monkeypatch.setattr(DeepgramTTSService, "_connect", nothing)
    monkeypatch.setattr(DeepgramTTSService, "_disconnect", nothing)
    return CachedDeepgramTTS(
        cache=cache, on_synth=lambda t, hit: synth.append((t.strip(), hit)), api_key="x"
    )


async def test_cache_hit_plays_in_order_and_keeps_heard_tracking_and_barge(
    monkeypatch: Any,
) -> None:
    synth: list[tuple[str, bool]] = []
    tts = _tts(FakeCache(b"\x10\x00" * 4800), synth, monkeypatch)
    down, _ = await run_test(
        tts,
        frames_to_send=[
            TTSSpeakFrame("What is the minimum payment?"),
            SentenceEndFrame("s1"),
            TTSSpeakFrame("And how many months?"),
            SentenceEndFrame("s2"),
        ],
    )
    assert synth == [("What is the minimum payment?", True), ("And how many months?", True)]
    order = (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame, SentenceEndFrame)
    kinds = [type(f).__name__ for f in down if isinstance(f, order)]
    first, second = kinds.index("SentenceEndFrame"), len(kinds) - 1
    # Each sentence: started, its audio, stopped, then its end marker (as live).
    for lo, hi in ((0, first), (first + 1, second)):
        part = kinds[lo:hi]
        assert part[0] == "TTSStartedFrame" and part[-1] == "TTSStoppedFrame"
        assert set(part[1:-1]) == {"TTSAudioRawFrame"}
    assert kinds[second] == "SentenceEndFrame"
    # Through the processor: s1's marker reached the output, then the rep barged.
    turns = Turns()
    proc = OrchestratorProcessor(turns, HeardTracker())  # type: ignore[arg-type]

    async def push(frame: Any, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        return None

    proc.push_frame = push  # type: ignore[method-assign]
    for sid, text in (("s1", "What is the minimum payment?"), ("s2", "And how many months?")):
        proc.tracker.queued(sid)
        proc._pending_sids.setdefault(text, deque()).append(sid)
    for f in down:
        if isinstance(f, SentenceEndFrame):
            proc.played(f.sid)
            break
    await proc.interrupt(reason="final", text="No, wait.")
    await asyncio.sleep(0.01)
    assert turns.heard == ["s1"] and turns.barges == [["s1"]]
    await proc.aclose()


async def test_a_miss_fills_the_cache_unless_it_has_numbers_or_was_cut(monkeypatch: Any) -> None:
    async def live(self: Any, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        yield None

    monkeypatch.setattr(DeepgramTTSService, "run_tts", live)
    synth: list[tuple[str, bool]] = []
    cache = FakeCache(b"")
    cache.get = lambda key: None  # type: ignore[method-assign]
    tts = _tts(cache, synth, monkeypatch)
    audio = TTSAudioRawFrame(b"\x01\x00" * 100, 24000, 1)
    for ctx, text in (("c1", "What is the balance?"), ("c2", "Is it forty five percent?")):
        async for _ in tts.run_tts(text, ctx):
            pass
        tts.observe(ctx, audio)
        tts.observe(ctx, TTSStoppedFrame(context_id=ctx))
    async for _ in tts.run_tts("Thanks for that.", "c3"):
        pass
    tts.observe("c3", audio)
    tts._capture.pop("c3")  # what on_audio_context_interrupted does on a barge
    tts.observe("c3", TTSStoppedFrame(context_id="c3"))
    assert cache.puts == [("What is the balance?", audio.audio)]
    assert [hit for _, hit in synth] == [False, False, False]


def test_budget_counts_live_characters_only() -> None:
    proc = OrchestratorProcessor(FakeTurns(), HeardTracker())  # type: ignore[arg-type]
    requests: list[str] = []
    hits: list[str | None] = []
    proc.on_tts_request = lambda sid, text: requests.append(sid)
    proc.on_tts_cache_hit = hits.append
    proc._pending_sids = {"Thanks.": deque(["s1"])}
    proc._pending_sids["Is it forty?"] = deque(["s2"])
    proc.synthesized("Thanks. ", True)
    proc.synthesized("Is it forty? ", False)
    proc.synthesized("Mm-hm. ", False)  # the ack's first live render: counted, no sid
    assert proc.tts_chars == len("Is it forty?") + len("Mm-hm.")
    assert proc.tts_cache_hits == 1 and hits == ["s1"] and requests == ["s2"]
