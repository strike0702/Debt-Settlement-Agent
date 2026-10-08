"""Blind pairwise naturalness judge for two eval run dirs. Secondary metric, NOT a gate.

CLI: ``python -m eval.judge_naturalness RUN_A RUN_B [--profile eval]
[--limit N] [--seed 0] [--human-pairs 20] [--out DIR]``.

For every scenario id completed (``status=ok``) in both runs, an LLM (role
``judge``: Claude Sonnet 5.5 on the ``eval`` / ``local`` profiles, no free-tier
fallback, needs ``ANTHROPIC_API_KEY``; Phase 30) sees the two transcripts as
"Transcript 1" / "Transcript 2", with no arm names, and picks the more natural
agent. Each pair is judged twice with the order swapped; a scenario counts as a
win only when both orders agree, otherwise it is a tie (position bias). Output:
``judge.json`` (per-scenario verdicts) and ``summary.json`` with A's win-rate
over decisive scenarios and its Wilson 95% CI. Also writes ``human_pairs.csv``
(20 random pairs, sides shuffled, blank rating column) and
``human_pairs_key.csv`` (which side is which
arm) so a human can check the judge.

This reverses ROADMAP §6's "no LLM-as-judge" for this one secondary metric only
(REVIEW_PLAN §2(c)). It reads run results; it never runs an agent.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.llm.client import make_client
from eval.metrics import load_scenario_results, wilson_interval
from eval.run_eval import RESULTS_ROOT, _build_settings

Verdict = Literal["A", "B", "tie"]

JUDGE_SYSTEM = """\
You compare two phone transcripts in which an automated agent negotiates a debt
settlement with a creditor's rep (REP). Judge ONLY how natural the AGENT sounds:
does it respond to what the rep actually said, sound like a competent human
negotiator, avoid robotic repetition and stiff phrasing, and keep turns concise?
Ignore who got the better deal and whether figures are correct. Reply with one
JSON object: {"winner": "1" | "2" | "tie", "reason": "<one short sentence>"}"""


class _JudgeReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    winner: str
    reason: str = ""


def _transcript(result: dict[str, Any]) -> str:
    """Render a call; older runs without ``transcript`` show agent lines only."""
    turns = result.get("transcript")
    if not turns:
        return "\n".join(f"AGENT: {line}" for line in result.get("agent_lines") or [])
    return "\n".join(
        f"{'AGENT' if t['role'] == 'agent' else 'REP'}: {t['text']}" for t in turns
    )


def paired_results(run_a: Path, run_b: Path) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(scenario_id, result_a, result_b) for ids completed in both runs, sorted by id."""
    a = {r["scenario_id"]: r for r in load_scenario_results(run_a) if r.get("status") == "ok"}
    b = {r["scenario_id"]: r for r in load_scenario_results(run_b) if r.get("status") == "ok"}
    return [(sid, a[sid], b[sid]) for sid in sorted(a.keys() & b.keys())]


async def _judge_once(llm: Any, first: str, second: str) -> str:
    """One ordered judgment: ``"1"``, ``"2"`` or ``"tie"`` (unparseable → tie)."""
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {
            "role": "user",
            "content": f"Transcript 1:\n{first}\n\nTranscript 2:\n{second}",
        },
    ]
    # Sonnet 5.5 thinking (low effort) counts against max_tokens; 400 could cut
    # the verdict off before the JSON line.
    raw = await llm.chat_text("judge", messages, max_tokens=1024)
    try:
        start, end = raw.find("{"), raw.rfind("}")
        reply = _JudgeReply.model_validate_json(raw[start : end + 1])
    except ValueError:
        return "tie"
    winner = reply.winner.strip().strip('"')
    return winner if winner in ("1", "2") else "tie"


async def judge_pair(llm: Any, text_a: str, text_b: str) -> dict[str, Any]:
    """Judge A-vs-B in both orders; a win needs both orders to agree."""
    ab = await _judge_once(llm, text_a, text_b)
    ba = await _judge_once(llm, text_b, text_a)
    vote_ab: Verdict = {"1": "A", "2": "B"}.get(ab, "tie")  # type: ignore[assignment]
    vote_ba: Verdict = {"1": "B", "2": "A"}.get(ba, "tie")  # type: ignore[assignment]
    verdict: Verdict = vote_ab if vote_ab == vote_ba else "tie"
    return {"a_first": vote_ab, "b_first": vote_ba, "verdict": verdict}


def summarize(verdicts: list[Verdict]) -> dict[str, Any]:
    """A's win-rate over decisive pairs, Wilson 95% CI, and the tie count."""
    wins = verdicts.count("A")
    losses = verdicts.count("B")
    decisive = wins + losses
    return {
        "n_pairs": len(verdicts),
        "a_wins": wins,
        "b_wins": losses,
        "ties": verdicts.count("tie"),
        "a_win_rate": wins / decisive if decisive else None,
        "a_win_rate_ci95": wilson_interval(wins, decisive),
        "a_win_rate_ties_half": (wins + 0.5 * verdicts.count("tie")) / len(verdicts)
        if verdicts
        else None,
    }


def write_human_pairs(
    out_dir: Path,
    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]],
    *,
    n: int,
    seed: int,
    label_a: str,
    label_b: str,
) -> tuple[Path, Path]:
    """CSV of ``n`` random pairs with shuffled sides, plus a separate answer key."""
    rng = random.Random(seed)
    chosen = rng.sample(pairs, min(n, len(pairs)))
    sheet = out_dir / "human_pairs.csv"
    key = out_dir / "human_pairs_key.csv"
    with sheet.open("w", newline="", encoding="utf-8") as fs, key.open(
        "w", newline="", encoding="utf-8"
    ) as fk:
        ws, wk = csv.writer(fs), csv.writer(fk)
        ws.writerow(
            ["pair_id", "scenario_id", "transcript_1", "transcript_2", "more_natural_1_2_tie"]
        )
        wk.writerow(["pair_id", "transcript_1_arm", "transcript_2_arm"])
        for i, (sid, ra, rb) in enumerate(chosen, 1):
            swap = rng.random() < 0.5
            left, right = (rb, ra) if swap else (ra, rb)
            ws.writerow([i, sid, _transcript(left), _transcript(right), ""])
            wk.writerow([i, *((label_b, label_a) if swap else (label_a, label_b))])
    return sheet, key


async def run_judge(
    run_a: Path,
    run_b: Path,
    *,
    llm: Any,
    out_dir: Path,
    limit: int | None = None,
    seed: int = 0,
    human_pairs: int = 20,
) -> dict[str, Any]:
    """Judge every shared scenario, write judge / summary / human CSVs; return summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = paired_results(run_a, run_b)
    if limit is not None:
        pairs = pairs[:limit]
    rows: list[dict[str, Any]] = []
    for sid, ra, rb in pairs:
        res = await judge_pair(llm, _transcript(ra), _transcript(rb))
        rows.append({"scenario_id": sid, **res})
    summary = {
        "run_a": str(run_a),
        "run_b": str(run_b),
        "agent_a": pairs[0][1].get("agent", "policy") if pairs else None,
        "agent_b": pairs[0][2].get("agent", "policy") if pairs else None,
        "gate": False,
        **summarize([r["verdict"] for r in rows]),
    }
    (out_dir / "judge.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_human_pairs(
        out_dir, pairs, n=human_pairs, seed=seed, label_a=run_a.name, label_b=run_b.name
    )
    return summary


def _resolve(run: str) -> Path:
    p = Path(run)
    return p if p.exists() else RESULTS_ROOT / run


async def _async_main(args: argparse.Namespace) -> int:
    settings = _build_settings(profile=args.profile, nlg="template")
    llm = make_client(settings)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else RESULTS_ROOT / f"judge_{stamp}"
    try:
        summary = await run_judge(
            _resolve(args.run_a),
            _resolve(args.run_b),
            llm=llm,
            out_dir=out_dir,
            limit=args.limit,
            seed=args.seed,
            human_pairs=args.human_pairs,
        )
    finally:
        await llm.aclose()
    print(json.dumps(summary, indent=2))
    print(f"wrote {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Blind pairwise naturalness judge (not a gate)")
    p.add_argument("run_a", help="run dir or run id under eval/results (arm A)")
    p.add_argument("run_b", help="run dir or run id under eval/results (arm B)")
    p.add_argument("--profile", default="eval")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--human-pairs", type=int, default=20, dest="human_pairs")
    p.add_argument("--out", default=None)
    return asyncio.run(_async_main(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
