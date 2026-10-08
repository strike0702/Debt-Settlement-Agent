#!/usr/bin/env python3
"""Live probe (paid): the demo NLU route with Claude Sonnet 5.5 first (Phase 41).

Runs ``app.agent.nlu.analyze`` on N lines of ``tests/nlu_corpus.jsonl`` through
the shipped ``demo`` profile (cache off), then one call with the daily budget
forced to 0 to show the fallback. Prints Sonnet latency p50 / p95, tokens and
cost per call, and how many calls failed over (e.g. hit the 6 s NLU timeout).

Spend control: the probe's own budget DB (``--db``, a scratch file, not the
app DB) is capped at ``--cap-usd`` through the same ``DailyBudget`` the demo
uses, and after ``--pilot`` calls the run stops if the projected total would
pass the cap. Needs ``ANTHROPIC_API_KEY`` (and a Groq key for the fallback).
Not part of CI; not imported by ``app/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.nlu import analyze  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.budget import micros_to_usd  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


async def _run(args: argparse.Namespace) -> int:
    lines = [json.loads(x) for x in (ROOT / "tests" / "nlu_corpus.jsonl").read_text().splitlines()]
    step = max(1, len(lines) // args.n)
    picked = lines[::step][: args.n]
    base = get_settings().model_copy(
        update={
            "llm_profile": "demo",
            "llm_cache": False,
            "db_path": args.db,
            "claude_daily_budget_usd": Decimal(args.cap_usd),
        }
    )
    metas: list[dict[str, Any]] = []
    client = LLMClient(base, on_call=metas.append)
    if "anthropic" not in client._keys:
        print("SKIP: no ANTHROPIC_API_KEY")
        return 2
    price = client._providers["anthropic"].prices["claude-sonnet-5-5"]
    cap_micros = int(Decimal(args.cap_usd) * 1_000_000)
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(picked, 1):
        before = len(metas)
        t0 = time.perf_counter()
        out = await analyze(line["text"], "", None, llm=client, settings=base)
        wall = (time.perf_counter() - t0) * 1000.0
        new = metas[before:]
        rows.append({"id": line["id"], "wall_ms": wall, "metas": new, "stance": out.stance})
        last = new[-1] if new else {}
        print(f"{line['id']:>5} {last.get('provider')}/{last.get('model')} "
              f"{wall:7.0f} ms  stance={out.stance}  errors={sum(1 for m in new if m['error'])}")
        if i == args.pilot:
            spent = client._budget_for().spent_micros()
            projected = spent * len(picked) // i
            print(f"pilot: spent ${micros_to_usd(spent)}, projected ${micros_to_usd(projected)}")
            if projected > cap_micros:
                print("STOP: projected spend over the cap")
                break
    status = client.budget_status()
    await client.aclose()

    ok = [m for r in rows for m in r["metas"] if m["provider"] == "anthropic" and not m["error"]]
    failed = [m for r in rows for m in r["metas"] if m["provider"] == "anthropic" and m["error"]]
    lat = [m["latency_ms"] for m in ok]
    costs = [price.cost_micros(m["prompt_tokens"] or 0, m["completion_tokens"] or 0) for m in ok]
    print("\n--- Sonnet NLU (demo route, effort low, 6 s NLU timeout) ---")
    print(f"calls ok={len(ok)} failed={len(failed)} lines={len(rows)}")
    for m in failed:
        print(f"  failed: {m['error'][:120]}")
    if lat:
        print(f"latency_ms p50={statistics.median(lat):.0f} p95={_pct(lat, 0.95):.0f} "
              f"max={max(lat):.0f}")
        print(f"tokens in mean={statistics.mean(m['prompt_tokens'] for m in ok):.0f} "
              f"out mean={statistics.mean(m['completion_tokens'] for m in ok):.0f} "
              f"out max={max(m['completion_tokens'] for m in ok)}")
        print(f"cost per call mean=${micros_to_usd(round(statistics.mean(costs)))} "
              f"max=${micros_to_usd(max(costs))}")
    print(f"budget DB: spent ${status['spent_usd']} of ${status['limit_usd']} ({status['day']})")

    # Forced exhaustion: the same route with a $0 budget must answer from Groq.
    forced: list[dict[str, Any]] = []
    zero = base.model_copy(
        update={"db_path": args.db + ".zero", "claude_daily_budget_usd": Decimal(0)}
    )
    client = LLMClient(zero, on_call=forced.append)
    out = await analyze(picked[0]["text"], "", None, llm=client, settings=zero)
    await client.aclose()
    print("\n--- forced budget-exhausted call ---")
    for m in forced:
        print(f"  {m.get('event') or ('failed' if m['error'] else 'ok')}: "
              f"{m['provider']}/{m['model']} failover_from={m['failover_from']} "
              f"{m['latency_ms']:.0f} ms")
    print(f"  stance={out.stance}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--pilot", type=int, default=3)
    ap.add_argument("--cap-usd", default="0.25")
    ap.add_argument("--db", required=True, help="scratch SQLite file for the probe's budget")
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
