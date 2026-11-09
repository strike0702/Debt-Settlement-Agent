"""Aura-2 pace on real agent lines, for the browser's reveal table (Phase 74; paid, about $0.05).

Speaks the demo's own sentences (taken from earlier harness runs, numbers
included, since number sentences are never in the TTS cache) with the agent
voice over REST through ``eval.voice.deepgram`` (ledgered), measures each
clip's length and its leading and trailing silence, and writes ``pace.json``.
``fit_pace.py`` turns that file into the table in ``web/src/lib/voice/reveal.ts``.

    uv run python docs/eval/voice_reveal_20261109/measure_pace.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from eval.voice import deepgram

SENTENCES = (
    "Hello, this is an automated agent calling on behalf of Synthetic Debt Relief "
    "about a client's account with you.",
    "You are speaking with an automated agent authorized to discuss settlement options.",
    "What payment terms can you work with for a settlement?",
    "What settlement percentage of the balance are you looking for?",
    "Do you need even payments, or can you allow a balloon or a flexible schedule?",
    "We will present this to the client and follow up after their review.",
    "I need to involve someone from our side.",
    "Does that fit your schedule?",
    "Thank you.",
    "Have a good day.",
    "Okay, even payments.",
    "Could you consider 31% of the balance, amounting to $387.50?",
    "Just to confirm, that's November 15 for the initial payment date?",
    "Great, 29% works.",
    "How about 1 payment of $362.50 starting November 15?",
    "We can propose 49% of the balance, which comes to $980.",
    "We can meet you partway at 59% of the balance, which is $1,180.",
    "Let's outline 6 payments totalling $1,300 beginning November 30.",
    "I have December 1 for the initial payment date.",
    "On October 31 the creditor payment is $216.66.",
    "On January 31, 2027 the creditor payment is $216.67.",
    "On March 31, 2027 the creditor payment is $216.67.",
    "Understood, up to 8 payments, at least $120 each.",
    "I suggest 4 payments totaling $1,140 beginning December 1.",
    "We can offer 29% of the balance, which totals $362.50.",
    "That is $1,005 in 12 payments.",
    "We could pay $99.99 a month.",
    "That is 45.5% of $2,000.",
    "The balance is $12,345.67 today.",
    "Then $75 from the 4th payment on.",
)
OUT = Path(__file__).with_name("pace.json")
FRAME = deepgram.SAMPLE_RATE // 100  # 10 ms


def silences_ms(pcm: bytes) -> tuple[int, int]:
    """Leading and trailing silence: 10 ms frames under 2% of the clip's peak level."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    n = len(x) // FRAME
    rms = np.sqrt((x[: n * FRAME].reshape(n, FRAME) ** 2).mean(axis=1))
    on = np.nonzero(rms > 0.02 * rms.max())[0]
    return int(on[0]) * 10, int(n - 1 - on[-1]) * 10


def main() -> None:
    rows = []
    for text in SENTENCES:
        pcm = deepgram.speak_pcm(text, voice=deepgram.AGENT_VOICE, note="phase 74 reveal pace")
        lead, trail = silences_ms(pcm)
        rows.append(
            {"text": text, "ms": round(len(pcm) / 2 / deepgram.SAMPLE_RATE * 1000),
             "lead_ms": lead, "trail_ms": trail}
        )  # fmt: skip
        print(rows[-1])
    OUT.write_text(json.dumps(rows, indent=1) + "\n")


if __name__ == "__main__":
    main()
