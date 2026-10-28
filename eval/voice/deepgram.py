"""Deepgram REST calls for the voice harness, each one logged to a usage ledger.

Two calls only: Aura-2 text-to-speech (fixtures, number-reading check) and
pre-recorded transcription (scoring how Aura-2 read a number back). The key
comes from ``DEEPGRAM_API_KEY`` in the environment or ``.env``; it is sent
only in the ``Authorization`` header and never printed, logged or put in a
URL. Every call appends characters or seconds and an estimated cost to
``LEDGER`` and refuses to run once the ledger passes ``CAP_USD`` (the phase's
paid cap). The live app does not use this module.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs/eval/voice_spike_20261028/deepgram_usage.jsonl"
CAP_USD = Decimal("5.00")
# Published list prices (verify before relying on them): Aura-2 per 1,000
# characters, Flux and Nova-3 pre-recorded per minute.
AURA2_PER_1K_CHARS = Decimal("0.030")
FLUX_PER_MIN = Decimal("0.0077")
NOVA3_PER_MIN = Decimal("0.0043")

REP_VOICE = "aura-2-arcas-en"
AGENT_VOICE = "aura-2-thalia-en"
SAMPLE_RATE = 16000


def api_key() -> str:
    """The Deepgram key from the environment or ``.env``; ``RuntimeError`` if unset."""
    key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if not key and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("DEEPGRAM_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set")
    return key


def spent_usd() -> Decimal:
    """Estimated spend recorded in the ledger so far."""
    if not LEDGER.exists():
        return Decimal(0)
    rows = [json.loads(x) for x in LEDGER.read_text().splitlines() if x.strip()]
    return sum((Decimal(r["usd"]) for r in rows), Decimal(0))


def record(
    kind: str, *, chars: int = 0, seconds: float = 0.0, usd: Decimal, note: str = ""
) -> None:
    """Append one usage row (also used by the spike for Flux minutes)."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,
        "chars": chars,
        "seconds": round(seconds, 2),
        "usd": str(usd.quantize(Decimal("0.000001"))),
        "note": note,
    }
    with LEDGER.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _guard() -> None:
    if spent_usd() >= CAP_USD:
        raise RuntimeError(f"Deepgram ledger is at the ${CAP_USD} cap; refusing a paid call")


def speak_pcm(text: str, *, voice: str = REP_VOICE, note: str = "") -> bytes:
    """Aura-2 TTS as raw 16-bit mono PCM at ``SAMPLE_RATE``."""
    _guard()
    resp = httpx.post(
        "https://api.deepgram.com/v1/speak",
        params={
            "model": voice,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "container": "none",
        },
        headers={"Authorization": f"Token {api_key()}"},
        json={"text": text},
        timeout=60,
    )
    resp.raise_for_status()
    usd = AURA2_PER_1K_CHARS * len(text) / 1000
    record("aura2_tts", chars=len(text), usd=usd, note=note or voice)
    return resp.content


def transcribe_wav(wav: bytes, *, seconds: float, note: str = "") -> str:
    """Nova-3 pre-recorded transcript of a WAV, words only (no smart format)."""
    _guard()
    resp = httpx.post(
        "https://api.deepgram.com/v1/listen",
        params={"model": "nova-3", "smart_format": "false", "punctuate": "true"},
        headers={"Authorization": f"Token {api_key()}", "Content-Type": "audio/wav"},
        content=wav,
        timeout=60,
    )
    resp.raise_for_status()
    usd = NOVA3_PER_MIN * Decimal(str(seconds)) / 60
    record("nova3_listen", seconds=seconds, usd=usd, note=note)
    alt = resp.json()["results"]["channels"][0]["alternatives"][0]
    return str(alt["transcript"])
