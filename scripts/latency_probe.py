#!/usr/bin/env python3
"""Latency probe: scripted WS turns against a running local server.

Drives ``/ws/call/{id}`` exactly like the browser does (``start`` →
``text`` or binary WAV → ``sentence_done`` for every ``say``), repeating a
short scripted ``easy_deal`` call until ``--turns`` rep turns have run. Reads
the server's per-turn ``latency`` events and reports p50 / p95 per stage, plus
the client-observed time from send to the first ``say``.

``--wav`` renders each scripted line with macOS ``say`` to 16 kHz WAV and
sends the bytes instead of text, so the turn includes server STT.
``--oracle`` attaches the scripted line's ``TurnAnalysis`` to each text turn,
for a server started with ``NLU_MODE=oracle``: every stage but NLU stays live
(used when the NLU provider's quota is spent).

Not a load test: one call at a time, one turn at a time, with ``--pause``
seconds of rep "think time" between turns. Run the server with
``LLM_CACHE=false`` so repeated lines are not served from the response cache.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import websockets

# Rep lines for easy_deal (rules: 8 payments, $100 min, even; ask 45%, floor 40%).
# Number words, not digits, so the same lines work through TTS → STT.
SCRIPT: tuple[str, ...] = (
    "We can do up to eight payments, minimum one hundred dollars each, even payments.",
    "We are looking for forty five percent of the balance.",
    "We could go to forty two percent.",
    "Okay, that works for us.",
    "Yes, that schedule is fine.",
)

# Ground-truth analyses for SCRIPT (``--oracle``), same order.
ORACLE: tuple[dict[str, Any], ...] = (
    {
        "stance": "info",
        "terms": [
            {"field": "max_payments", "value": 8, "quote": "eight payments"},
            {"field": "min_payment_cents", "value": 10000, "quote": "one hundred dollars"},
            {"field": "payment_structure", "value": "even", "quote": "even payments"},
        ],
    },
    {"stance": "offer", "settlement_ask_pct": 45.0, "ask_quote": "forty five percent"},
    {"stance": "counter", "settlement_ask_pct": 42.0, "ask_quote": "forty two percent"},
    {"stance": "accept"},
    {"stance": "accept"},
)

STAGES: tuple[str, ...] = (
    "stt_ms",
    "nlu_ms",
    "engine_ms",
    "policy_ms",
    "nlg_ms",
    "queue_ms",
    "server_total_ms",
    "client_first_say_ms",
)


def percentile(xs: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100); ``None`` when empty."""
    if not xs:
        return None
    s = sorted(xs)
    k = (p / 100.0) * (len(s) - 1)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def render_wavs(lines: tuple[str, ...], out_dir: Path) -> list[bytes]:
    """Render each line with macOS ``say`` to 16 kHz mono 16-bit WAV."""
    wavs: list[bytes] = []
    for i, line in enumerate(lines):
        path = out_dir / f"line_{i}.wav"
        subprocess.run(
            ["say", "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@16000", line],
            check=True,
        )
        wavs.append(path.read_bytes())
    return wavs


async def _drain_turn(ws: Any, t_send: float) -> tuple[list[dict[str, Any]], float | None]:
    """Read events until ``turn_done``; return them and ms to the first ``say``."""
    events: list[dict[str, Any]] = []
    first_say: float | None = None
    while True:
        msg = json.loads(await ws.recv())
        events.append(msg)
        if msg.get("type") == "say" and first_say is None:
            first_say = (time.perf_counter() - t_send) * 1000.0
        if msg.get("type") == "error":
            raise RuntimeError(f"server error: {msg.get('message')}")
        if msg.get("type") == "turn_done":
            # STT failures and empty transcripts also end with turn_done, no say.
            if any(m.get("type") in ("latency", "stt_error") for m in events):
                return events, first_say
            if not any(m.get("type") == "transcript" for m in events):
                return events, first_say


async def _ack(ws: Any, events: list[dict[str, Any]]) -> None:
    """Ack every spoken sentence (as the browser does after TTS)."""
    for ev in events:
        if ev.get("type") == "say":
            await ws.send(json.dumps({"type": "sentence_done", "id": ev["id"]}))
            while json.loads(await ws.recv()).get("type") != "turn_done":
                pass


async def probe(
    url: str,
    scenario: str,
    turns: int,
    *,
    wavs: list[bytes] | None,
    pause_s: float,
    oracle: bool = False,
) -> list[dict[str, Any]]:
    """Run calls until ``turns`` rep turns finished; return one sample per turn."""
    samples: list[dict[str, Any]] = []
    while len(samples) < turns:
        call_id = f"probe-{uuid.uuid4().hex[:8]}"
        async with websockets.connect(f"{url.rstrip('/')}/ws/call/{call_id}") as ws:
            await ws.send(json.dumps({"type": "start", "scenario_id": scenario}))
            opening, _ = await _drain_turn(ws, time.perf_counter())
            await _ack(ws, opening)
            for i, line in enumerate(SCRIPT):
                if len(samples) >= turns:
                    break
                await asyncio.sleep(pause_s)
                t_send = time.perf_counter()
                if wavs is not None:
                    await ws.send(wavs[i])
                else:
                    payload: dict[str, Any] = {"type": "text", "text": line}
                    if oracle:
                        payload["oracle"] = ORACLE[i]
                    await ws.send(json.dumps(payload))
                events, first_say = await _drain_turn(ws, t_send)
                lat = next((e for e in events if e.get("type") == "latency"), None)
                phase = next(
                    (e for e in reversed(events) if e.get("type") == "phase"), {}
                )
                sample = {k: (lat or {}).get(k) for k in STAGES}
                sample["client_first_say_ms"] = first_say
                sample["intent"] = phase.get("intent")
                sample["call_id"] = call_id
                samples.append(sample)
                print(
                    f"turn {len(samples):>2} {sample['intent'] or '-':<18} "
                    + " ".join(
                        f"{k.removesuffix('_ms')}={v:.0f}"
                        for k, v in sample.items()
                        if k.endswith("_ms") and isinstance(v, (int, float))
                    ),
                    file=sys.stderr,
                )
                await _ack(ws, events)
                if phase.get("intent") in ("PROPOSE_WRAP", "NO_DEAL_WRAP", "ESCALATE", "CLOSE"):
                    break
            await ws.send(json.dumps({"type": "end"}))
    return samples


def summarize(samples: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    """p50 / p95 / n per stage over turns where the stage was reported."""
    out: dict[str, dict[str, float | int | None]] = {}
    for stage in STAGES:
        vals = [float(s[stage]) for s in samples if isinstance(s.get(stage), (int, float))]
        out[stage] = {"p50": percentile(vals, 50), "p95": percentile(vals, 95), "n": len(vals)}
    return out


def render_table(summary: dict[str, dict[str, float | int | None]]) -> str:
    """Markdown ``stage | p50 | p95 | n`` table."""
    lines = ["| stage | p50 ms | p95 ms | n |", "|---|---|---|---|"]
    for stage, row in summary.items():
        p50, p95 = row["p50"], row["p95"]
        fmt = lambda v: "n/a" if v is None else f"{v:.0f}"  # noqa: E731
        lines.append(f"| {stage} | {fmt(p50)} | {fmt(p95)} | {row['n']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="ws://127.0.0.1:8000")
    ap.add_argument("--scenario", default="easy_deal")
    ap.add_argument("--turns", type=int, default=20)
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between rep turns")
    ap.add_argument("--wav", action="store_true", help="send macOS `say` WAVs (voice turns)")
    ap.add_argument("--oracle", action="store_true", help="send oracle NLU (NLU_MODE=oracle)")
    ap.add_argument("--out", type=Path, help="write samples + summary JSON here")
    args = ap.parse_args(argv)
    if args.oracle and args.wav:
        ap.error("--oracle needs text turns (the WAV path carries no oracle)")

    wavs = None
    if args.wav:
        with tempfile.TemporaryDirectory() as d:
            wavs = render_wavs(SCRIPT, Path(d))
    samples = asyncio.run(
        probe(
            args.url, args.scenario, args.turns, wavs=wavs, pause_s=args.pause, oracle=args.oracle
        )
    )
    summary = summarize(samples)
    print(render_table(summary))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"samples": samples, "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
