"""One command for the voice harness: ``uv run python -m eval.voice``.

    uv run python -m eval.voice --target spike --out docs/eval/voice_spike_20261028/spike
    uv run python -m eval.voice --target pipecat-ui --audio speakers \
        --out docs/eval/voice_client_20261029/pipecat-ui-speakers

Starts the target's server on a free port (never 8056 / 8071), opens Chrome,
plays every scripted call (``eval.voice.script``), stops everything, and
writes to ``--out``: ``raw.jsonl`` (page, server and driver events),
``timeline.jsonl`` (neutral events), ``turns.json`` (one row per rep line),
``summary.json`` and ``summary.md``. Scoring lives in ``eval.voice.metrics``;
driving in ``eval.voice.driver``. Missing fixture WAVs are an error: build them
with ``python -m eval.voice.fixtures`` (paid).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from eval.voice import driver, metrics
from eval.voice.script import CALLS, call_by_id


def _ensure_fixtures() -> None:
    missing = [
        ln.wav_name for c in CALLS for ln in c.lines if not (driver.FIXTURES / ln.wav_name).exists()
    ]
    if missing:
        raise SystemExit(f"missing fixtures {missing}; run: uv run python -m eval.voice.fixtures")


def _build_web() -> None:
    """Always rebuild: the UI target must run the client in this checkout."""
    web = driver.ROOT / "web"
    if not (web / "node_modules").is_dir():
        subprocess.run(["npm", "ci"], cwd=web, check=True)
    subprocess.run(["npm", "run", "build"], cwd=web, check=True, stdout=subprocess.DEVNULL)


async def run(
    target: str, audio: str, call_ids: list[str], out: Path, repeat: int = 1
) -> dict[str, Any]:
    _ensure_fixtures()
    calls = [call_by_id(c) for c in call_ids] * repeat
    port = driver.free_port()
    out.mkdir(parents=True, exist_ok=True)
    runs: list[driver.CallRun] = []
    chrome_args: list[str] = []
    log = out / "server.log"
    if target in ("pipecat", "pipecat-ui"):
        if target == "pipecat-ui":
            _build_web()
        play = driver.run_pipecat_ui if target == "pipecat-ui" else driver.run_pipecat
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "audit.db"
            async with driver.app_server(port, log, db) as base:
                for call in calls:
                    runs.append(
                        await play(
                            call, base=base, audio=audio, chrome_args=chrome_args
                        )  # type: ignore[arg-type]
                    )
    else:
        async with driver.spike_server(port, log) as base:
            for call in calls:
                runs.append(
                    await driver.run_spike(call, base=base, audio=audio, chrome_args=chrome_args)
                )  # type: ignore[arg-type]
    return write(out, target, audio, calls, runs)


def write(
    out: Path, target: str, audio: str, calls: list, runs: list[driver.CallRun]
) -> dict[str, Any]:
    rows: list[metrics.TurnRow] = []
    phantoms: list[dict] = []
    barges: list[dict] = []
    seen: dict[str, int] = {}
    for r in runs:  # label repeats: numbers_easy#1, numbers_easy#2, ...
        seen[r.call] = seen.get(r.call, 0) + 1
        r.call = f"{r.call}#{seen[r.call]}"
    with (out / "raw.jsonl").open("w") as raw, (out / "timeline.jsonl").open("w") as tl:
        for call, r in zip(calls, runs, strict=True):
            for src, evs in (("page", r.page), ("server", r.server), ("driver", r.driver)):
                for e in evs:
                    raw.write(json.dumps({"call": r.call, "src": src, **e}) + "\n")
            norm = driver.normalize(r)
            for e in norm:
                tl.write(json.dumps({"call": r.call, **e}) + "\n")
            rows += metrics.score_call(r.call, call.lines, norm)
            phantoms += [{"call": r.call, **e} for e in metrics.phantom_finals(call.lines, norm)]
            barges += [{"call": r.call, **e} for e in metrics.false_barges(call.lines, norm)]
    summary = metrics.summarize(rows, phantoms=len(phantoms), false_barge_count=len(barges))
    notes = [f"{r.call}: {n}" for r in runs for n in r.notes]
    doc = {
        "target": target,
        "audio": audio,
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "summary": summary,
        "phantoms": phantoms,
        "false_barges": barges,
        "notes": notes,
    }
    (out / "turns.json").write_text(json.dumps(metrics.rows_as_dicts(rows), indent=1))
    (out / "summary.json").write_text(json.dumps(doc, indent=1))
    (out / "summary.md").write_text(render_md(doc, rows))
    return doc


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.0f}" if abs(v) >= 10 else f"{v:.2f}"
    return str(v)


def render_md(doc: dict[str, Any], rows: list[metrics.TurnRow]) -> str:
    s = doc["summary"]
    lines = [
        f"# Voice harness: {doc['target']} ({doc['audio']} audio)",
        "",
        f"Run {doc['date']}. Times are ms from the end of the rep's clip unless noted.",
        "",
        "| metric | value |",
        "|---|---|",
        *[f"| {k} | {_fmt(v)} |" for k, v in s.items()],
        "",
        "## Turns",
        "",
        "| call | line | kind | eot | final | reply | tts byte | first audio | any audio "
        "| barged | stop | numbers | WER | heard |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.call} | {r.line} | {r.kind} | {_fmt(r.eot_ms)} | {_fmt(r.final_ms)} | "
            f"{_fmt(r.reply_ms)} | {_fmt(r.tts_byte_ms)} | {_fmt(r.first_audio_ms)} | "
            f"{_fmt(r.any_audio_ms)} | "
            f"{_fmt(r.barged)} | {_fmt(r.stop_ms)} | {r.num_hit}/{r.num_total} | {_fmt(r.wer)} | "
            f"{r.hyp[:70] or '–'} |"
        )
    if doc["phantoms"]:
        lines += ["", "## Phantom rep lines", ""] + [
            f"- {p['call']}: {p['text']!r}" for p in doc["phantoms"]
        ]
    if doc["false_barges"]:
        lines += ["", f"## False barge-ins: {len(doc['false_barges'])}"]
    if doc["notes"]:
        lines += ["", "## Driver notes", ""] + [f"- {n}" for n in doc["notes"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Voice measurement harness")
    targets = ["spike", "pipecat", "pipecat-ui"]
    ap.add_argument("--target", choices=targets, required=True)
    ap.add_argument("--audio", choices=["inject", "speakers"], default="inject")
    ap.add_argument("--calls", default="all", help="comma-separated call ids, or all")
    ap.add_argument("--repeat", type=int, default=1, help="play the call list this many times")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--gap-s", type=float, default=0.0, help="extra silence before each reply")
    ap.add_argument("--blip-before", help="line id: stall the server just before it")
    ap.add_argument("--blip-s", type=float, default=8.0, help="stall length")
    ap.add_argument(
        "--ice", choices=["all", "relay"], default="all", help="pipecat-ui: relay forces TURN"
    )
    ap.add_argument(
        "--claude-nlu",
        action="store_true",
        help="paid: Claude Haiku reads the rep first, as on the hosted demo ($1 daily cap)",
    )
    a = ap.parse_args()
    driver.ICE_POLICY = a.ice
    if a.claude_nlu:
        driver.CLAUDE_NLU_BUDGET_USD = "1.00"
    driver.SOAK = driver.Soak(a.gap_s, a.blip_before, a.blip_s)
    ids = [c.id for c in CALLS] if a.calls == "all" else a.calls.split(",")
    doc = asyncio.run(run(a.target, a.audio, ids, a.out, a.repeat))
    print(json.dumps(doc["summary"], indent=1))
    for n in doc["notes"]:
        print("note:", n)


if __name__ == "__main__":
    main()
