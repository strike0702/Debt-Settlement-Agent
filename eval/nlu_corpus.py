"""NLU corpus eval: per-flag precision/recall and term exact-match on live NLU.

Runs every line of ``tests/nlu_corpus.jsonl`` through ``app.agent.nlu.analyze``
(real LLM, ``demo`` profile by default, ``llm_cache=True`` so reruns per model
are free) and scores the post-verified output against the hand labels. Writes
the ``## <LABEL>`` section of ``docs/eval/nlu_corpus.md`` (BEFORE kept above
AFTER) plus raw per-line predictions in ``docs/eval/nlu_corpus_<label>.jsonl``.

Each line runs inside ``llm_call_scope("corpus:<id>")`` with an audit hook, so
every LLM call is written to ``--audit-db`` (default
``eval/results/nlu_corpus_audit.db``, git-ignored).

Not the policy eval (``eval.run_eval``): no orchestrator, no simulator.
CLI: ``python -m eval.nlu_corpus --label BEFORE [--profile demo]``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from app.agent.nlu import VerifiedAnalysis, analyze
from app.config import get_settings
from app.domain.fields import FIELDS_BY_NAME
from app.llm.call_audit import audit_llm_calls, llm_call_scope
from app.llm.client import LLMUnavailable, make_client
from app.store.audit import AuditLog

CORPUS_PATH = Path("tests/nlu_corpus.jsonl")
REPORT_PATH = Path("docs/eval/nlu_corpus.md")
CACHE_PATH = "eval/nlu_corpus_cache.db"
AUDIT_PATH = Path("eval/results/nlu_corpus_audit.db")
REF = date(2026, 4, 1)
SECTION_ORDER = ("BEFORE", "AFTER", "DEFAULT_EFFORT", "LOW_EFFORT")

FLAGS = (
    "asks_client_private_info",
    "demands_commitment",
    "firm",
    "wants_to_end",
    "hostility",
)

# Line ``agent`` aliases → the agent line the rep is replying to.
AGENT_LINES: dict[str, str] = {
    "default": "What settlement terms can you accept on this account?",
    "confirm": (
        "Fifty percent works for us. We can schedule four payments totaling "
        "two thousand dollars, starting May first. Does that work?"
    ),
    "counter": "We can offer fifty percent of the balance, which is two thousand dollars.",
    "min_ask": FIELDS_BY_NAME["min_payment_cents"].ask_text,
}


def load_corpus(path: Path = CORPUS_PATH) -> list[dict[str, Any]]:
    """Read the JSONL corpus; one labelled rep utterance per line."""
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def expected_flags(line: dict[str, Any]) -> dict[str, bool]:
    """Hand labels as booleans (missing ⇒ False; hostility ≥ 0.5 ⇒ hostile)."""
    out = {f: bool(line.get(f, False)) for f in FLAGS}
    out["hostility"] = float(line.get("hostility", 0.0)) >= 0.5
    return out


def predicted_flags(out: VerifiedAnalysis, hostility_threshold: float) -> dict[str, bool]:
    """Policy-relevant booleans; hostility binarized at the escalation threshold."""
    return {
        "asks_client_private_info": out.asks_client_private_info,
        "demands_commitment": out.demands_commitment,
        "firm": out.firm,
        "wants_to_end": out.wants_to_end,
        "hostility": out.hostility >= hostility_threshold,
    }


def expected_terms(line: dict[str, Any]) -> dict[str, Any]:
    """Term labels: field values (cents / int / enum / ISO date), ask %, cents clarify."""
    out: dict[str, Any] = dict(line.get("terms", {}))
    if line.get("ask_pct") is not None:
        out["settlement_ask_pct"] = float(line["ask_pct"])
    if line.get("cents_ambiguity") is not None:
        out["cents_ambiguity"] = int(line["cents_ambiguity"])
    return out


def predicted_terms(out: VerifiedAnalysis) -> dict[str, Any]:
    """Same shape as ``expected_terms`` from a post-verified analysis."""
    terms: dict[str, Any] = {}
    for t in out.terms:
        terms[t.field] = t.value.isoformat() if isinstance(t.value, date) else t.value
    if out.settlement_ask_pct is not None:
        terms["settlement_ask_pct"] = float(out.settlement_ask_pct)
    if out.cents_ambiguity_bare is not None:
        terms["cents_ambiguity"] = out.cents_ambiguity_bare
    return terms


def _pr(tp: int, fp: int, fn: int) -> tuple[float | None, float | None]:
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return prec, rec


def score(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-flag / per-stance P/R, term exact-match, filler false accepts."""
    done = [r for r in records if not r.get("skipped")]
    summary: dict[str, Any] = {
        "n": len(records),
        "skipped": len(records) - len(done),
        "flags": {},
        "stance": {},
    }

    def _binary(name: str, exp_key: str, pred_key: str) -> dict[str, Any]:
        tp = fp = fn = 0
        fps: list[str] = []
        fns: list[str] = []
        for r in done:
            e, p = r["expected"][exp_key], r["predicted"][pred_key]
            if e and p:
                tp += 1
            elif p:
                fp += 1
                fps.append(r["id"])
            elif e:
                fn += 1
                fns.append(r["id"])
        prec, rec = _pr(tp, fp, fn)
        return {
            "pos": tp + fn, "tp": tp, "fp": fp, "fn": fn,
            "precision": prec, "recall": rec, "fp_ids": fps, "fn_ids": fns,
        }

    for r in done:
        for s in ("accept", "reject"):
            r["expected"][f"stance_{s}"] = r["expected"]["stance"] == s
            r["predicted"][f"stance_{s}"] = r["predicted"]["stance"] == s
    for f in FLAGS:
        summary["flags"][f] = _binary(f, f, f)
    for s in ("accept", "reject"):
        summary["stance"][s] = _binary(s, f"stance_{s}", f"stance_{s}")
    summary["stance_accuracy"] = (
        sum(r["expected"]["stance"] == r["predicted"]["stance"] for r in done) / len(done)
        if done else None
    )

    term_lines = [r for r in done if r["expected"]["terms"]]
    misses = [r["id"] for r in done if r["expected"]["terms"] != r["predicted"]["terms"]]
    summary["terms"] = {
        "exact_all": (len(done) - len(misses)) / len(done) if done else None,
        "exact_with_terms": (
            sum(r["expected"]["terms"] == r["predicted"]["terms"] for r in term_lines)
            / len(term_lines)
            if term_lines else None
        ),
        "n_with_terms": len(term_lines),
        "miss_ids": misses,
    }

    filler = [r for r in done if "filler" in r["tags"]]
    false_acc = [
        r["id"] for r in filler
        if r["predicted"]["stance"] == "accept" and r["expected"]["stance"] != "accept"
    ]
    summary["filler"] = {"n": len(filler), "false_accept": len(false_acc), "ids": false_acc}
    return summary


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def render_section(label: str, summary: dict[str, Any], meta: dict[str, Any]) -> str:
    """Markdown section for one run (tables + miss ids for triage)."""
    lines = [
        f"## {label}",
        "",
        f"- git: `{meta['git']}`  profile=`{meta['profile']}`  ref={REF.isoformat()}",
        f"- lines: {summary['n']}  skipped (LLM unavailable): {summary['skipped']}",
        "- model share: "
        + (", ".join(f"{m}={c}" for m, c in meta["models"].most_common()) or "none"),
        "",
        "| label | pos | TP | FP | FN | precision | recall |",
        "|---|---|---|---|---|---|---|",
    ]
    rows = [*summary["flags"].items()] + [
        (f"stance={k}", v) for k, v in summary["stance"].items()
    ]
    for name, m in rows:
        lines.append(
            f"| {name} | {m['pos']} | {m['tp']} | {m['fp']} | {m['fn']} "
            f"| {_fmt(m['precision'])} | {_fmt(m['recall'])} |"
        )
    t, fl = summary["terms"], summary["filler"]
    lines += [
        "",
        "| metric | value |",
        "|---|---|",
        f"| stance accuracy (all 8 labels) | {_fmt(summary['stance_accuracy'])} |",
        f"| term exact-match (all lines) | {_fmt(t['exact_all'])} |",
        f"| term exact-match (lines with terms, n={t['n_with_terms']}) "
        f"| {_fmt(t['exact_with_terms'])} |",
        f"| filler false accepts (n={fl['n']}) | {fl['false_accept']} |",
        "",
        "Misses (line ids):",
        "",
    ]
    for name, m in rows:
        if m["fp_ids"] or m["fn_ids"]:
            lines.append(
                f"- {name}: FP {', '.join(m['fp_ids']) or '-'}; "
                f"FN {', '.join(m['fn_ids']) or '-'}"
            )
    lines.append(f"- terms: {', '.join(t['miss_ids']) or '-'}")
    lines.append(f"- filler false accept: {', '.join(fl['ids']) or '-'}")
    return "\n".join(lines) + "\n"


_HEADER = """\
# NLU corpus eval

Hand-labelled synthetic rep utterances (`tests/nlu_corpus.jsonl`) run through
`app.agent.nlu.analyze` with a live LLM, scored after post-verification.
Generated by `python -m eval.nlu_corpus --label <LABEL>`; per-line predictions
are in `nlu_corpus_<label>.jsonl` next to this file.
Hostility is counted as positive when it reaches the escalation threshold.
"""


def write_report(path: Path, label: str, section: str) -> None:
    """Replace (or add) the ``## label`` section; keep BEFORE above AFTER."""
    sections: dict[str, str] = {}
    if path.exists():
        parts = re.split(r"(?m)^(?=## )", path.read_text())
        for part in parts[1:]:
            sections[part.split("\n", 1)[0][3:].strip()] = part.rstrip() + "\n"
    sections[label] = section
    order = [s for s in SECTION_ORDER if s in sections] + [
        s for s in sections if s not in SECTION_ORDER
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER + "\n" + "\n".join(sections[s] for s in order))


async def run_corpus(
    corpus: list[dict[str, Any]],
    *,
    profile: str,
    concurrency: int = 4,
    audit: AuditLog | None = None,
    llm: Any | None = None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Analyze every line; returns per-line records and answering-model counts.

    With ``audit``, each line's LLM calls are audited under ``corpus:<id>``.
    ``llm`` overrides the client built from ``profile`` (tests).
    """
    settings = get_settings().model_copy(
        update={
            "llm_profile": profile,
            "llm_cache": True,
            "llm_cache_path": CACHE_PATH,
            "nlu_mode": "llm",
        }
    )
    models: Counter[str] = Counter()
    line_models: dict[str, str] = {}
    current: dict[int, str] = {}

    def on_call(meta: dict[str, Any]) -> None:
        tid = id(asyncio.current_task())
        if meta.get("model") and tid in current:
            line_models[current[tid]] = f"{meta.get('provider')}/{meta['model']}"

    hook = audit_llm_calls(audit, then=on_call) if audit is not None else on_call
    owns_llm = llm is None
    if llm is None:
        llm = make_client(settings, on_call=hook)
    else:
        llm.on_call = hook
    sem = asyncio.Semaphore(concurrency)

    async def one(line: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            current[id(asyncio.current_task())] = line["id"]
            agent = AGENT_LINES[line.get("agent", "default")]
            rec: dict[str, Any] = {
                "id": line["id"],
                "tags": line.get("tags", []),
                "text": line["text"],
                "expected": {
                    **expected_flags(line),
                    "stance": line["stance"],
                    "terms": expected_terms(line),
                },
            }
            try:
                with llm_call_scope(f"corpus:{line['id']}"):
                    out = await analyze(
                        line["text"], agent, line.get("pending"),
                        llm=llm, settings=settings, ref=REF,
                    )
            except LLMUnavailable as e:
                rec["skipped"] = True
                rec["error"] = str(e)[:200]
                return rec
            rec["predicted"] = {
                **predicted_flags(out, settings.hostility_threshold),
                "stance": out.stance,
                "terms": predicted_terms(out),
            }
            rec["model"] = line_models.get(line["id"], "fast_path")
            return rec

    try:
        records = await asyncio.gather(*(one(ln) for ln in corpus))
    finally:
        aclose = getattr(llm, "aclose", None)
        if owns_llm and aclose is not None:
            await aclose()
    for r in records:
        if not r.get("skipped"):
            models[r["model"]] += 1
    return list(records), models


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    """CLI entry: run, score, write report section + per-line JSONL."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True, help="report section, e.g. BEFORE / AFTER")
    ap.add_argument("--profile", default="demo")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--audit-db", type=Path, default=AUDIT_PATH)
    args = ap.parse_args(argv)

    corpus = load_corpus()
    args.audit_db.parent.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(args.audit_db)
    try:
        records, models = asyncio.run(
            run_corpus(
                corpus, profile=args.profile, concurrency=args.concurrency, audit=audit
            )
        )
    finally:
        audit.close()
    summary = score(records)
    meta = {"git": _git_sha(), "profile": args.profile, "models": models}
    write_report(REPORT_PATH, args.label, render_section(args.label, summary, meta))
    raw = REPORT_PATH.parent / f"nlu_corpus_{args.label.lower()}.jsonl"
    raw.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
    print(render_section(args.label, summary, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
