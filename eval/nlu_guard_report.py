"""Stance-guard report: raw LLM stance vs ``repair_stance`` output (Phase 37).

Reads per-line records written by ``eval.nlu_corpus`` (``nlu_corpus_<label>.jsonl``,
which carry ``predicted.stance_raw`` and ``stance_rule`` since Phase 37) and
renders Markdown: per-class stance precision / recall / F1 with and without the
guard, every line where the guard changed the label (helped / hurt / neutral),
and a per-rule tally. Pure scoring: no LLM call, no change to ``app/``.

``context_rows`` handles older records with no raw stance (e.g. the Phase 32
Gemini run): it can only list lines where a phrase rule would force the label,
since the raw stance there was never stored.

CLI: ``python -m eval.nlu_guard_report RECORDS.jsonl [--context OLD.jsonl]``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.agent.nlu import (
    _INJECTION_RE,
    _has_unguarded_accept_phrase,
    _has_unguarded_reject_phrase,
)

STANCES = ("offer", "counter", "accept", "reject", "stall", "info", "question", "other")
RULES = ("injection", "reject_phrase", "accept_phrase", "short_ack")


def load_records(path: Path) -> list[dict[str, Any]]:
    """Answered records from a per-line JSONL (skipped lines dropped)."""
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return [r for r in rows if not r.get("skipped")]


def _f1(p: float | None, r: float | None) -> float | None:
    if p is None or r is None or p + r == 0:
        return None if p is None or r is None else 0.0
    return 2 * p * r / (p + r)


def per_class(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """One-vs-rest P/R/F1 per stance label; ``key`` is ``stance`` or ``stance_raw``."""
    out: dict[str, dict[str, Any]] = {}
    for s in STANCES:
        tp = sum(r["expected"]["stance"] == s and r["predicted"][key] == s for r in records)
        fp = sum(r["expected"]["stance"] != s and r["predicted"][key] == s for r in records)
        fn = sum(r["expected"]["stance"] == s and r["predicted"][key] != s for r in records)
        p = tp / (tp + fp) if tp + fp else None
        rc = tp / (tp + fn) if tp + fn else None
        out[s] = {"pos": tp + fn, "tp": tp, "fp": fp, "fn": fn,
                  "precision": p, "recall": rc, "f1": _f1(p, rc)}
    return out


def accuracy(records: list[dict[str, Any]], key: str) -> float | None:
    """Share of lines whose ``key`` stance equals the hand label."""
    if not records:
        return None
    return sum(r["expected"]["stance"] == r["predicted"][key] for r in records) / len(records)


def filler_false_accepts(records: list[dict[str, Any]], key: str) -> list[str]:
    """Filler-tagged lines labelled accept that are not accepts."""
    return [
        r["id"] for r in records
        if "filler" in r["tags"]
        and r["predicted"][key] == "accept" and r["expected"]["stance"] != "accept"
    ]


def changed_lines(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lines where the guard changed the stance, with a helped / hurt / neutral verdict."""
    out = []
    for r in records:
        raw, rep = r["predicted"]["stance_raw"], r["predicted"]["stance"]
        exp = r["expected"]["stance"]
        if raw == rep:
            continue
        verdict = "helped" if rep == exp else "hurt" if raw == exp else "neutral"
        out.append({"id": r["id"], "text": r["text"], "expected": exp, "raw": raw,
                    "guarded": rep, "rule": r.get("stance_rule", "?"), "verdict": verdict})
    return out


def rule_tally(records: list[dict[str, Any]]) -> dict[str, Counter[str]]:
    """Per rule: lines it decided, how many it changed, helped, hurt."""
    tally: dict[str, Counter[str]] = {rule: Counter() for rule in RULES}
    changed = {c["id"]: c for c in changed_lines(records)}
    for r in records:
        rule = r.get("stance_rule")
        if rule not in tally:
            continue
        tally[rule]["fired"] += 1
        if r["id"] in changed:
            tally[rule]["changed"] += 1
            tally[rule][changed[r["id"]]["verdict"]] += 1
    return tally


def context_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lines where a phrase rule forces the label (raw stance unknown; old records)."""
    out = []
    for r in records:
        text = r["text"]
        if _INJECTION_RE.search(text):
            continue  # injection only changes an LLM accept; cannot tell without raw
        if _has_unguarded_reject_phrase(text):
            rule = "reject_phrase"
        elif _has_unguarded_accept_phrase(text):
            rule = "accept_phrase"
        else:
            continue
        out.append({"id": r["id"], "text": text, "rule": rule,
                    "expected": r["expected"]["stance"], "guarded": r["predicted"]["stance"],
                    "correct": r["expected"]["stance"] == r["predicted"]["stance"]})
    return out


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def render(records: list[dict[str, Any]]) -> str:
    """Markdown tables: per-class with/without guard, changed lines, rule tally."""
    on, off = per_class(records, "stance"), per_class(records, "stance_raw")
    lines = [
        f"Lines: {len(records)}. Stance accuracy (8 labels): guard on "
        f"{_fmt(accuracy(records, 'stance'))}, guard off {_fmt(accuracy(records, 'stance_raw'))}. "
        f"Filler false accepts: on {len(filler_false_accepts(records, 'stance'))}, "
        f"off {len(filler_false_accepts(records, 'stance_raw'))}.",
        "",
        "| stance | pos | P on | R on | F1 on | P off | R off | F1 off |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in STANCES:
        a, b = on[s], off[s]
        lines.append(
            f"| {s} | {a['pos']} | {_fmt(a['precision'])} | {_fmt(a['recall'])} "
            f"| {_fmt(a['f1'])} | {_fmt(b['precision'])} | {_fmt(b['recall'])} "
            f"| {_fmt(b['f1'])} |"
        )
    lines += ["", "| rule | decided | changed | helped | hurt | neutral |",
              "|---|---|---|---|---|---|"]
    for rule, c in rule_tally(records).items():
        lines.append(f"| {rule} | {c['fired']} | {c['changed']} | {c['helped']} "
                     f"| {c['hurt']} | {c['neutral']} |")
    lines += ["", "| id | line | label | raw LLM | guarded | rule | verdict |",
              "|---|---|---|---|---|---|---|"]
    for c in changed_lines(records):
        lines.append(f"| {c['id']} | {c['text']} | {c['expected']} | {c['raw']} "
                     f"| {c['guarded']} | {c['rule']} | {c['verdict']} |")
    return "\n".join(lines) + "\n"


def render_context(records: list[dict[str, Any]]) -> str:
    """Markdown for records without raw stance: forced lines and whether they are right."""
    rows = context_rows(records)
    lines = [
        f"Lines where a phrase rule forces the label: {len(rows)} "
        f"(right {sum(r['correct'] for r in rows)}, wrong {sum(not r['correct'] for r in rows)}).",
        "",
        "| id | line | label | guarded | rule |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['id']} | {r['text']} | {r['expected']} | {r['guarded']} "
                     f"| {r['rule']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI: print the guard report for RECORDS (and optional old-run context)."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("records", type=Path)
    ap.add_argument("--context", type=Path, default=None,
                    help="older nlu_corpus_<label>.jsonl without stance_raw")
    args = ap.parse_args(argv)
    print(render(load_records(args.records)))
    if args.context is not None:
        print(render_context(load_records(args.context)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
