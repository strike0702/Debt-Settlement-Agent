"""Speech-to-text via the shared LLM client ``stt`` role, plus a hallucination screen.

``transcribe`` is a thin timing wrapper around ``LLMClient.transcribe``.
``transcribe_screened`` also drops transcripts that are Whisper
hallucinations rather than speech: on noise or the agent's own TTS echo,
Whisper invents text, often words from its bias prompt ("and payment,
payment, payment."). A dropped clip is treated like an empty transcript by the
caller. This module does not choose providers or models (routing stays in
``app.llm.client``) and never sees or stores raw audio beyond passing it on.
The WebSocket call handler (``app.voice.ws``) is the caller.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from app.llm.client import WHISPER_PROMPT, SttSegment, Transcription

# Whisper's own silence rule (openai/whisper ``transcribe`` defaults): a
# segment with no_speech_prob above 0.6 *and* avg_logprob below -1.0 is silence.
NO_SPEECH_PROB_MIN = 0.6
SILENT_AVG_LOGPROB_MAX = -1.0
# Below this avg_logprob a segment is noise whatever no_speech_prob says
# (Whisper already treats < -1.0 as a failed decode and retries).
GARBLED_AVG_LOGPROB_MAX = -1.5
# Whisper's repetition-loop check: gzip ratio above 2.4 means looping text.
LOOP_COMPRESSION_RATIO_MIN = 2.4

# Hallucinations Whisper is known to emit on silence (normalized text).
KNOWN_HALLUCINATIONS = frozenset(
    {
        "you",
        "thank you",
        "thanks",
        "thank you very much",
        "thank you so much",
        "thanks for watching",
        "thank you for watching",
        "bye",
        "bye bye",
    }
)
# Words that carry no answer on their own.
FILLER_WORDS = frozenset(
    {"a", "ah", "and", "er", "hmm", "mhm", "mm", "oh", "so", "the", "uh", "um", "umm", "you"}
)
_WORD_RE = re.compile(r"[a-z0-9$%']+")


def _stem(word: str) -> str:
    return word[:-1] if word.endswith("s") else word


PROMPT_STEMS = frozenset(_stem(w) for w in _WORD_RE.findall(WHISPER_PROMPT.lower()))


def _silent(seg: SttSegment) -> bool:
    if seg.no_speech_prob > NO_SPEECH_PROB_MIN and seg.avg_logprob < SILENT_AVG_LOGPROB_MAX:
        return True
    if seg.avg_logprob < GARBLED_AVG_LOGPROB_MAX:
        return True
    return seg.compression_ratio is not None and seg.compression_ratio > LOOP_COMPRESSION_RATIO_MIN


def segments_drop_reason(segments: tuple[SttSegment, ...] | None) -> str | None:
    """``"no_speech"`` when every scored segment looks like silence, noise or a loop."""
    if not segments:
        return None
    return "no_speech" if all(_silent(s) for s in segments) else None


def text_drop_reason(text: str) -> str | None:
    """Why ``text`` is a hallucination rather than an answer, or ``None`` to keep it.

    Drops a known silence phrase ("Thank you."), filler alone ("um"), and
    bias-prompt words with a repeat ("and payment, payment."). One prompt word
    or phrase ("Balloon.", "Monthly payments.") and anything with another word,
    a digit or a sign ("It's three", "$35", "No") is kept.
    """
    words = _WORD_RE.findall(text.lower())
    if not words:
        return None
    if " ".join(words) in KNOWN_HALLUCINATIONS:
        return "known_hallucination"
    if all(w in FILLER_WORDS for w in words):
        return "filler"
    stems = [_stem(w) for w in words if w not in FILLER_WORDS]
    if all(s in PROMPT_STEMS for s in stems) and max(Counter(stems).values()) > 1:
        return "prompt_echo"
    return None


def drop_reason(result: Transcription) -> str | None:
    """Segment check first (needs ``verbose_json``), then the text check."""
    return segments_drop_reason(result.segments) or text_drop_reason(result.text)


class _Transcriber(Protocol):
    async def transcribe(self, wav_bytes: bytes, prompt: str | None = None) -> str: ...


@dataclass(frozen=True)
class ScreenedTranscript:
    """STT outcome: ``text`` is ``""`` when the clip was dropped (``dropped`` says why)."""

    text: str
    stt_ms: float
    dropped: str | None = None
    raw_text: str = ""


async def transcribe(
    llm: _Transcriber | Any,
    wav_bytes: bytes,
    *,
    prompt: str | None = None,
) -> tuple[str, float]:
    """Return ``(text, stt_ms)`` from a 16 kHz WAV blob."""
    t0 = time.perf_counter()
    text = await llm.transcribe(wav_bytes, prompt)
    stt_ms = (time.perf_counter() - t0) * 1000.0
    return text, stt_ms


async def transcribe_screened(
    llm: _Transcriber | Any,
    wav_bytes: bytes,
    *,
    prompt: str | None = None,
) -> ScreenedTranscript:
    """Transcribe, then drop Whisper hallucinations; ``LLMUnavailable`` propagates."""
    t0 = time.perf_counter()
    detailed = getattr(llm, "transcribe_detailed", None)
    if detailed is not None:
        result: Transcription = await detailed(wav_bytes, prompt)
    else:
        result = Transcription(await llm.transcribe(wav_bytes, prompt))
    stt_ms = (time.perf_counter() - t0) * 1000.0
    reason = drop_reason(result) if result.text.strip() else None
    if reason is not None:
        return ScreenedTranscript("", stt_ms, dropped=reason, raw_text=result.text)
    return ScreenedTranscript(result.text, stt_ms, raw_text=result.text)
