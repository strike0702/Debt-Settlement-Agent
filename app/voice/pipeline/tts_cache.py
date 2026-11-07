"""Exact-text audio cache for Aura-2 sentences that carry no number (Phase 70).

Most agent lines repeat word for word across calls (discovery questions,
read-back prompts, the instant ack). A cache hit plays at once instead of
waiting about 0.35 s for Deepgram's first byte, and costs nothing.

Only sentences with no digits, no money / percent signs and no number words
are cached (``cacheable``; DECISIONS D18): a sentence with a number always goes
to live TTS, and nothing is spliced. The key is TTS model + voice + sample rate
+ encoding + exact text. Entries are raw PCM files in one directory next to
``DB_PATH`` (``cache_dir``), bounded in total size; the least recently used go
first. Any disk error is a miss, never a failed call.

``CachedDeepgramTTS`` is the pipeline's TTS service (``session``): a hit is
fed through the same audio context as live audio (``TTSStartedFrame``, audio,
``TTSStoppedFrame``), so ordering behind earlier sentences, ``SentenceEndFrame``
"heard" tracking, the output gate's pause and barge-in drops behave exactly as
with live TTS. A miss speaks live and keeps the audio when it completes
uninterrupted. Accounting (live characters vs hits) is the owner's, through
``on_synth``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from pipecat.frames.frames import Frame, TTSAudioRawFrame, TTSStoppedFrame
from pipecat.services.deepgram.tts import DeepgramTTSService

from app.agent.numbers import NUMBER_WORDS
from app.config import Settings

# NUMBER_WORDS (the template guard's lexicon) plus the larger ordinals and scales.
_EXTRA_NUMBER_WORDS = frozenset(
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth "
    "twentieth thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth "
    "hundredth thousandth millionth billion billionth trillion twice percent".split()
)
_SPOKEN_NUMBERS = NUMBER_WORDS | _EXTRA_NUMBER_WORDS
_WORD_RE = re.compile(r"[a-z]+")
_NUMBER_SIGNS = set("$%€£¥#")
# A hit is played as frames of this length (mono 16-bit PCM).
HIT_FRAME_S = 0.04


def cacheable(text: str) -> bool:
    """True when ``text`` has no digit, money / percent sign or number word."""
    if any(c.isdigit() or c in _NUMBER_SIGNS for c in text):
        return False
    return not any(w in _SPOKEN_NUMBERS for w in _WORD_RE.findall(text.lower()))


def cache_dir(s: Settings) -> Path:
    """``voice_tts_cache_dir``, else ``tts_cache/`` beside the database file."""
    if s.voice_tts_cache_dir:
        return Path(s.voice_tts_cache_dir)
    return Path(s.db_path).resolve().parent / "tts_cache"


class SentenceAudioCache:
    """PCM per exact text on disk, at most ``max_bytes`` in total (LRU eviction)."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._index: dict[Path, int] | None = None  # path → size, oldest use first

    @staticmethod
    def key(*, model: str, voice: str, sample_rate: int, encoding: str, text: str) -> str:
        """Cache key: everything that changes the audio, and the exact text."""
        raw = f"{model}\n{voice}\n{sample_rate}\n{encoding}\n{text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _load(self) -> dict[Path, int]:
        if self._index is None:
            files: list[tuple[float, Path, int]] = []
            with contextlib.suppress(OSError):
                self.root.mkdir(parents=True, exist_ok=True)
                for p in self.root.glob("*.pcm"):
                    st = p.stat()
                    files.append((st.st_mtime, p, st.st_size))
            self._index = {p: size for _, p, size in sorted(files)}
        return self._index

    def get(self, key: str) -> bytes | None:
        """The audio for ``key``, or None (also on any disk error)."""
        index = self._load()
        path = self.root / f"{key}.pcm"
        if path not in index:
            return None
        try:
            audio = path.read_bytes()
            os.utime(path)
        except OSError:
            index.pop(path, None)
            return None
        index[path] = index.pop(path)  # most recently used last
        return audio

    def put(self, key: str, text: str, audio: bytes) -> None:
        """Store ``audio`` unless ``text`` is not cacheable or it cannot fit."""
        if not cacheable(text) or not audio or len(audio) > self.max_bytes:
            return
        index = self._load()
        path = self.root / f"{key}.pcm"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(audio)
            os.replace(tmp, path)
        except OSError:
            return
        index.pop(path, None)
        index[path] = len(audio)
        while sum(index.values()) > self.max_bytes:
            old = next(iter(index))
            del index[old]
            with contextlib.suppress(OSError):
                old.unlink()

    @property
    def size(self) -> int:
        return sum(self._load().values())


# One cache per directory, shared by every session of the process.
_SHARED: dict[Path, SentenceAudioCache] = {}


def shared_cache(root: Path, max_bytes: int) -> SentenceAudioCache:
    """The process-wide cache for ``root`` (bound updated to ``max_bytes``)."""
    cache = _SHARED.setdefault(root, SentenceAudioCache(root, max_bytes))
    cache.max_bytes = max_bytes
    return cache


# on_synth(text, hit): one sentence sent to TTS, from the cache or live.
OnSynth = Callable[[str, bool], None]


class CachedDeepgramTTS(DeepgramTTSService):
    """Aura-2 over Deepgram's websocket, with number-free sentences served from the cache."""

    def __init__(
        self,
        *,
        cache: SentenceAudioCache | None,
        on_synth: OnSynth | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.cache = cache
        self.on_synth = on_synth
        self._hits: set[str] = set()  # context ids served from the cache
        self._capture: dict[str, tuple[str, str, bytearray]] = {}  # ctx → key, text, audio

    def _key(self, text: str) -> str:
        st = self._settings
        return SentenceAudioCache.key(
            model=str(st.model or st.voice),
            voice=str(st.voice),
            sample_rate=self.sample_rate,
            encoding=self._encoding,
            text=text,
        )

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        """A hit yields the cached audio into the context; a miss speaks live."""
        use = self.cache is not None and cacheable(text)
        key = self._key(text) if use else ""
        audio = self.cache.get(key) if use and self.cache is not None else None
        if self.on_synth is not None:
            self.on_synth(text, audio is not None)
        if audio is not None:
            self._hits.add(context_id)
            # Small frames, like Deepgram's stream: the output gate paces and
            # pauses per frame, so a big frame would make a pause land late.
            step = max(2, int(self.sample_rate * HIT_FRAME_S) * 2)
            for i in range(0, len(audio), step):
                chunk = audio[i : i + step]
                yield TTSAudioRawFrame(chunk, self.sample_rate, 1, context_id=context_id)
            # Live, Deepgram's "Flushed" ends the context; here the end is ours.
            yield TTSStoppedFrame(context_id=context_id)
            return
        if use:
            # Contexts play one at a time: an older capture never completes now.
            self._capture = {context_id: (key, text, bytearray())}
        async for frame in super().run_tts(text, context_id):
            yield frame

    async def flush_audio(self, context_id: str | None = None) -> None:
        """Never ask Deepgram to flush a cached sentence: its "Flushed" would end the next one."""
        if context_id is not None and context_id in self._hits:
            self._hits.discard(context_id)
            return
        await super().flush_audio(context_id)

    async def append_to_audio_context(self, context_id: str | None, frame: Any) -> None:
        if context_id is not None:
            self.observe(context_id, frame)
        await super().append_to_audio_context(context_id, frame)

    def observe(self, context_id: str, frame: Any) -> None:
        """Collect a live sentence's audio; store it once Deepgram says it is complete."""
        cap = self._capture.get(context_id)
        if cap is None:
            return
        if isinstance(frame, TTSAudioRawFrame):
            cap[2].extend(frame.audio)
        elif isinstance(frame, TTSStoppedFrame):
            del self._capture[context_id]
            if self.cache is not None:
                self.cache.put(cap[0], cap[1], bytes(cap[2]))

    async def on_audio_context_interrupted(self, context_id: str) -> None:
        """A barge cut the sentence: its audio is partial, never cache it."""
        self._capture.pop(context_id, None)
        self._hits.discard(context_id)
        await super().on_audio_context_interrupted(context_id)
