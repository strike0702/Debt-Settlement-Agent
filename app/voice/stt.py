"""Speech-to-text via the shared LLM client ``stt`` role.

Thin timing wrapper around ``LLMClient.transcribe`` / ``FakeLLM.transcribe``.
Does not choose providers or models — routing stays in ``app.llm.client``.
WebSocket call handler is the only caller in phase 10.
"""

from __future__ import annotations

import time
from typing import Any, Protocol


class _Transcriber(Protocol):
    async def transcribe(self, wav_bytes: bytes, prompt: str | None = None) -> str: ...


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
