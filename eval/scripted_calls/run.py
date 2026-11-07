"""Run the scripted calls (see the package docstring) and report every turn's move.

``run_script`` returns one record per rep turn: the line, the agent's move
(intent, reason, PUBLIC facts), whether it met the turn's ``expect``, the NLU
time and the acts the model returned. ``main`` runs every script (or one) with
``--nlu oracle | flat | acts`` and writes ``<out>/scripted_<nlu>.json`` plus a
short table on stdout. Live runs record prompt / completion tokens per call
through the client's ``on_call`` hook. Eval only.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app.agent.nlu_types import TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.config import Settings, get_settings
from app.domain.scenario import load_scenario, resolve_scenario_dir
from app.llm.client import make_client

SCRIPTS_DIR = Path(__file__).parent / "scripts"
DEFAULT_REF = date(2026, 10, 9)


@dataclass
class ScriptResult:
    """One script's outcome: per-turn records, skip note, and LLM call metas."""

    name: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.skipped is None and all(t["ok"] for t in self.turns)


def load_script(path: Path) -> dict[str, Any]:
    """Read one YAML script."""
    return yaml.safe_load(path.read_text())


def requirement_missing(script: dict[str, Any]) -> str | None:
    """Why a script cannot run on this tree, or None."""
    if script.get("requires") == "phase69" and importlib.util.find_spec(
        "app.domain.rule_replies"
    ) is None:
        return "needs Phase 69 (operator rules) on main; not merged"
    return None


def _fact_value(fact: Any) -> Any:
    v = fact.value
    return v.isoformat() if isinstance(v, date) else v


def check_move(expect: dict[str, Any] | None, move: dict[str, Any]) -> bool:
    """True when the move matches ``expect`` (intent, reason, listed facts)."""
    if not expect:
        return True
    intents = expect["intent"] if isinstance(expect["intent"], list) else [expect["intent"]]
    if move["intent"] not in intents:
        return False
    if "reason" in expect and move["reason"] != expect["reason"]:
        return False
    return all(move["facts"].get(k) == v for k, v in expect.get("facts", {}).items())


def scripted_settings(nlu: str, base: Settings | None = None, **over: Any) -> Settings:
    """Settings for one run: template NLG, auto-ack, NLU mode / schema from ``nlu``."""
    s = base or get_settings()
    upd: dict[str, Any] = {"nlg_mode": "template", "db_path": ":memory:", "llm_cache": False}
    if nlu == "oracle":
        upd |= {"nlu_mode": "oracle", "llm_profile": "offline"}
    else:
        upd |= {"nlu_mode": "llm", "nlu_schema": nlu}
    return s.model_copy(update={**upd, **over})


async def run_script(
    script: dict[str, Any], *, settings: Settings, llm: Any | None = None, nlu: str = "oracle"
) -> ScriptResult:
    """Drive one scripted call; never raises on a wrong move (it is recorded)."""
    res = ScriptResult(name=script["name"])
    missing = requirement_missing(script)
    if missing:
        res.skipped = missing
        return res
    ref = date.fromisoformat(str(script.get("ref", DEFAULT_REF)))
    call = load_scenario(resolve_scenario_dir(script["scenario"]), rebase_to=ref)
    orch = Orchestrator(CallSession(scenario=call), llm=llm, settings=settings, auto_ack=True)
    await orch.start()
    for i, turn in enumerate(script["turns"]):
        oracle = None
        if nlu == "oracle":
            oracle = TurnAnalysis.model_validate(turn.get("oracle") or {"stance": "info"})
        utt = await orch.on_creditor_text(turn["rep"], oracle=oracle)
        verified = orch._trace_ctx.get("verified")
        move = {
            "intent": utt.action.intent.value,
            "reason": utt.action.reason,
            "facts": {k: _fact_value(f) for k, f in utt.action.facts.items()},
        }
        res.turns.append(
            {
                "i": i,
                "rep": turn["rep"],
                "move": move,
                "said": " ".join(t for _, t in utt.sentences),
                "expect": turn.get("expect"),
                "ok": check_move(turn.get("expect"), move),
                "nlu_ms": utt.timings.get("nlu_ms"),
                "acts": getattr(verified, "acts", None),
                "unaccounted": getattr(verified, "unaccounted_numbers", None),
            }
        )
    return res


def all_scripts(only: str | None = None) -> list[dict[str, Any]]:
    """Every script in name order (or just ``only``)."""
    paths = sorted(SCRIPTS_DIR.glob("*.yaml"))
    scripts = [load_script(p) for p in paths]
    return [s for s in scripts if only is None or s["name"] == only]


def summarize(results: list[ScriptResult]) -> dict[str, Any]:
    """Turn and script pass counts, NLU latency of live turns, tokens."""
    ran = [r for r in results if r.skipped is None]
    turns = [t for r in ran for t in r.turns]
    live = [c for r in ran for c in r.calls if not c.get("error")]
    nlu_ms = [t["nlu_ms"] for t in turns if t["nlu_ms"] is not None]
    return {
        "scripts_passed": sum(r.passed for r in ran),
        "scripts_run": len(ran),
        "skipped": {r.name: r.skipped for r in results if r.skipped},
        "turns_ok": sum(t["ok"] for t in turns),
        "turns": len(turns),
        "failed": [
            f"{r.name}#{t['i']}" for r in ran for t in r.turns if not t["ok"]
        ],
        "nlu_ms_p50": statistics.median(nlu_ms) if nlu_ms else None,
        "calls": len(live),
        "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in live),
        "completion_tokens": sum(c.get("completion_tokens") or 0 for c in live),
    }


async def _run_all(args: argparse.Namespace) -> list[ScriptResult]:
    settings = scripted_settings(args.nlu, llm_profile=args.profile) if args.nlu != "oracle" else (
        scripted_settings("oracle")
    )
    metas: list[dict[str, Any]] = []
    llm = None
    if args.nlu != "oracle":
        llm = make_client(settings, on_call=metas.append, providers_path=args.providers)
    results = []
    try:
        for script in all_scripts(args.script):
            start = len(metas)
            res = await run_script(script, settings=settings, llm=llm, nlu=args.nlu)
            res.calls = metas[start:]
            results.append(res)
    finally:
        if llm is not None and hasattr(llm, "aclose"):
            await llm.aclose()
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI: run scripts, print a table, write JSON; exit 1 if any turn missed."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nlu", choices=("oracle", "flat", "acts"), default="oracle")
    ap.add_argument("--providers", type=Path, default=None)
    ap.add_argument("--profile", default="demo")
    ap.add_argument("--script", default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.nlu != "oracle" and args.providers is None:
        ap.error("live runs need --providers (a pinned file)")
    results = asyncio.run(_run_all(args))
    for r in results:
        if r.skipped:
            print(f"SKIP {r.name}: {r.skipped}")
            continue
        print(f"{'PASS' if r.passed else 'FAIL'} {r.name}")
        for t in r.turns:
            if not t["ok"]:
                print(f"  #{t['i']} {t['rep']!r} -> {t['move']['intent']}:{t['move']['reason']}"
                      f" {t['move']['facts']} expected {t['expect']} | acts={t['acts']}")
    summary = summarize(results)
    print(json.dumps({k: v for k, v in summary.items() if k != "failed"}))
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": summary,
            "scripts": [
                {"name": r.name, "skipped": r.skipped, "turns": r.turns, "calls": r.calls}
                for r in results
            ],
        }
        (args.out / f"scripted_{args.nlu}.json").write_text(
            json.dumps(payload, indent=1, default=str)
        )
    return 0 if summary["turns_ok"] == summary["turns"] else 1


if __name__ == "__main__":
    sys.exit(main())
