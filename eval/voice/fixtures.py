"""Build the scripted rep WAVs in ``fixtures/voice/`` (once; they are committed).

Each ``RepLine`` of ``eval.voice.script`` becomes a 16 kHz mono 16-bit WAV
spoken by Aura-2 (``REP_VOICE``). Lines with ``parts`` are joined by
``pause_ms`` of silence. Leading and trailing silence is trimmed
(``LEAD_MS`` / ``TAIL_MS`` kept) so a clip's end is the end of speech: the harness measures
latency from it. Existing files are kept unless ``--force``; generating
costs Deepgram credit (logged by ``eval.voice.deepgram``).
"""

from __future__ import annotations

import argparse
import array
import io
import wave
from pathlib import Path

from eval.voice import deepgram
from eval.voice.script import RepLine, all_lines

FIXTURES = deepgram.ROOT / "fixtures/voice"
LEAD_MS = 150
TAIL_MS = 80
_WINDOW_MS = 10
_SILENCE_RMS = 60  # int16 RMS of a 10 ms window below which it counts as silent


def trim(pcm: bytes, *, rate: int = deepgram.SAMPLE_RATE) -> bytes:
    """Trim leading/trailing silence of 16-bit PCM by 10 ms windows.

    Keeps ``LEAD_MS`` before the first voiced window (soft onsets such as
    "No," sit below any per-sample threshold) and ``TAIL_MS`` after the last.
    """
    samples = array.array("h", pcm)
    win = rate * _WINDOW_MS // 1000
    voiced = [
        i
        for i in range(0, len(samples) - win + 1, win)
        if (sum(s * s for s in samples[i : i + win]) / win) ** 0.5 > _SILENCE_RMS
    ]
    if not voiced:
        return pcm
    lo = max(0, voiced[0] - rate * LEAD_MS // 1000)
    hi = min(len(samples), voiced[-1] + win + rate * TAIL_MS // 1000)
    return samples[lo:hi].tobytes()


def wav_bytes(pcm: bytes, *, rate: int = deepgram.SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def render_line(line: RepLine) -> bytes:
    """One line as WAV bytes (paid Aura-2 call per part)."""
    if not line.parts:
        return wav_bytes(trim(deepgram.speak_pcm(line.text, note=line.id)))
    gap = b"\x00\x00" * (deepgram.SAMPLE_RATE * line.pause_ms // 1000)
    pcm = gap.join(trim(deepgram.speak_pcm(p, note=line.id)) for p in line.parts)
    return wav_bytes(pcm)


def build(*, force: bool = False, out: Path = FIXTURES) -> list[Path]:
    """Write every missing (or, with ``force``, every) line WAV; return the paths written."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for line in all_lines():
        path = out / line.wav_name
        if path.exists() and not force:
            continue
        path.write_bytes(render_line(line))
        written.append(path)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="regenerate existing WAVs")
    args = ap.parse_args()
    for p in build(force=args.force):
        print("wrote", p.relative_to(deepgram.ROOT))
    print(f"Deepgram ledger total: ${deepgram.spent_usd()}")


if __name__ == "__main__":
    main()
