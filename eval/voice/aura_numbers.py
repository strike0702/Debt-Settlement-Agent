"""How Aura-2 reads our money, percents and dates (paid; a few cents per run).

Each case is an agent line in exactly the formats ``app.domain.units`` renders
today (``$1,250.50``, ``45.5%``, ``June 1, 2027``, ``2nd``). The line is spoken
by Aura-2 with the agent voice, transcribed back by Nova-3 with smart
formatting off (words, not digits), and the transcript's numbers are compared
with what a listener should hear. A miss means Aura-2 read the figure wrong or
ambiguously; Phase 63 adds a spoken form in ``Fact.render()`` only for those.
The transcript is a proxy for a listener: read the table, not just the score.

    uv run python -m eval.voice.aura_numbers --out docs/eval/voice_spike_20261028
"""

from __future__ import annotations

import argparse
import json
import wave
from io import BytesIO
from pathlib import Path

from eval.voice import deepgram
from eval.voice.fixtures import wav_bytes
from eval.voice.metrics import numbers_in

# (agent line, numbers a listener must hear, as canonical tokens)
CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("That is 8 payments of $120 each.", ("<8>", "<120>")),
    ("We can offer $1,250.50 in total.", ("<1250>", "<50>")),
    ("That works out to $562.50, or 45% of the balance.", ("<562>", "<50>", "<45>")),
    ("Could you do 45.5% instead?", ("<45.5>",)),
    ("That is 65.25% of $2,000.", ("<65.25>", "<2000>")),
    ("We could pay $99.99 a month.", ("<99>", "<99>")),
    ("The balance is $12,345.67 today.", ("<12345>", "<67>")),
    ("The first payment would be on November 15.", ("<d:11-15>",)),
    ("The last payment would be on June 1, 2027.", ("<d:6-1>", "<2027>")),
    ("So 6 payments, starting December 1.", ("<6>", "<d:12-1>")),
    ("Then $75 from the 4th payment on.", ("<75>",)),
    ("That is $1,005 in 12 payments.", ("<1005>", "<12>")),
)


def _seconds(wav: bytes) -> float:
    with wave.open(BytesIO(wav)) as w:
        return w.getnframes() / w.getframerate()


def _score(text: str, heard: str, want: tuple[str, ...]) -> dict:
    got = numbers_in(heard)
    return {
        "text": text,
        "heard": heard,
        "want": want,
        "missing": [t for t in want if t not in got],
    }


def run(out: Path, *, rescore: bool = False) -> list[dict]:
    """Speak and transcribe every case (or, with ``rescore``, re-read the saved
    transcripts for free); write ``aura_numbers.{json,md}`` to ``out``."""
    rows = []
    saved = {}
    if rescore:
        saved = {r["text"]: r["heard"] for r in json.loads((out / "aura_numbers.json").read_text())}
    for text, want in CASES:
        if text in saved:
            rows.append(_score(text, saved[text], want))
            continue
        wav = wav_bytes(deepgram.speak_pcm(text, voice=deepgram.AGENT_VOICE, note="aura numbers"))
        heard = deepgram.transcribe_wav(wav, seconds=_seconds(wav), note="aura numbers")
        rows.append(_score(text, heard, want))
    out.mkdir(parents=True, exist_ok=True)
    (out / "aura_numbers.json").write_text(json.dumps(rows, indent=1))
    ok = sum(1 for r in rows if not r["missing"])
    md = [
        "# Aura-2 number reading",
        "",
        f"Voice `{deepgram.AGENT_VOICE}`, transcribed by Nova-3 (smart format off). "
        f"{ok}/{len(rows)} lines with every number heard.",
        "",
        "| agent line (as rendered today) | Nova-3 heard | missing |",
        "|---|---|---|",
    ]
    md += [f"| {r['text']} | {r['heard']} | {' '.join(r['missing']) or 'none'} |" for r in rows]
    (out / "aura_numbers.md").write_text("\n".join(md) + "\n")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Aura-2 number reading check (paid)")
    ap.add_argument("--out", type=Path, default=deepgram.ROOT / "docs/eval/voice_spike_20261028")
    ap.add_argument("--rescore", action="store_true", help="re-score saved transcripts (free)")
    a = ap.parse_args()
    rows = run(a.out, rescore=a.rescore)
    for r in rows:
        print(("OK  " if not r["missing"] else "MISS"), r["text"], "->", r["heard"])
    print(f"Deepgram ledger total: ${deepgram.spent_usd()}")


if __name__ == "__main__":
    main()
