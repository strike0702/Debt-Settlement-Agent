"""NLU corpus eval: per-flag precision/recall and term exact-match on live NLU.

Runs every line of ``tests/nlu_corpus.jsonl`` through ``app.agent.nlu.analyze``
(real LLM, ``demo`` profile by default, ``llm_cache=True`` so reruns per model
are free) and scores the post-verified output against the hand labels. Writes
the ``## <LABEL>`` section of ``docs/eval/nlu_corpus.md`` (BEFORE kept above
AFTER) plus raw per-line predictions in ``docs/eval/nlu_corpus_<label>.jsonl``.

Each line runs inside ``llm_call_scope("corpus:<id>")`` with an audit hook, so
every LLM call is written to ``--audit-db`` (default
``eval/results/nlu_corpus_audit.db``, git-ignored).

Lines the LLM could not answer (``LLMUnavailable`` or any other error) get one
more try after a backoff. If any line is still unanswered the CLI exits 2,
prints the missing ids and writes no report section: a partial run silently
scores a different denominator (Phase 28 lost 58 lines at concurrency 4).

Each record keeps the repaired stance (``predicted.stance``, what the agent
uses) and the raw LLM stance (``predicted.stance_raw``, parsed from the same
reply before ``repair_stance``), plus which guard rule decided
(``stance_rule``). ``--no-repair-stance`` scores the raw stance instead; with
``--from-records`` it rescores a saved JSONL without any LLM call, so one paid
pass yields both variants (Phase 37). Records also keep the raw LLM ``firm``
flag (``predicted.firm_raw``) and ``has_terms``; ``--from-records
--rescore-guards`` re-runs the current ``repair_stance`` / ``repair_firm`` on
the saved raw values, so a guard change is measured without new calls
(Phase 45). Eval-only: ``app/`` is unchanged.
``--providers`` swaps ``config/providers.yaml`` for this run (like
``eval.run_eval``). Without it, paid ``budgeted: true`` targets (the demo NLU's
Claude, Phase 41) are dropped unless ``--allow-budgeted`` is passed, so the
default ``demo`` profile never spends money by accident; an explicit
``--providers`` file is used as is (that is how approved paid runs are made).

One row must be one model. A fail-over (e.g. the ``demo`` NLU route's 6 s
timeout to the next provider) or a JSON retry on another model would mix
models inside a labelled row, so by default the CLI exits 2 when more than one
``provider/model`` answered (each record's ``answered_by``) and writes no
report section; ``--allow-mixed-models`` writes the row and says so in it
(Phase 38). Fast-path lines (no LLM call) never count as a model.

Lines may also label ``ask_total_cents`` (a dollar-total ask) and
``amount_ambiguous`` (total vs per payment unclear); both score as terms
(Phase 39, ``tests/nlu_corpus_amounts.jsonl``).

Not the policy eval (``eval.run_eval``): no orchestrator, no simulator.
CLI: ``python -m eval.nlu_corpus --label BEFORE [--profile demo] [--corpus PATH]
[--providers PATH] [--no-repair-stance] [--from-records PATH [--rescore-guards]]
[--allow-mixed-models]``.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

# Private helpers are imported read-only to attribute which stance guard fired;
# the eval never changes how the agent repairs stance.
from app.agent.nlu import (
    _INJECTION_RE,
    VerifiedAnalysis,
    _ack_dominant,
    _has_unguarded_accept_phrase,
    _has_unguarded_reject_phrase,
    _parse_analysis,
    analyze,
    repair_firm,
    repair_stance,
)
from app.config import get_settings
from app.domain.fields import FIELDS_BY_NAME
from app.llm.call_audit import audit_llm_calls, llm_call_scope
from app.llm.client import LLMUnavailable, make_client
from app.store.audit import AuditLog
from eval.run_eval import _DEFAULT_PROVIDERS, without_budgeted

CORPUS_PATH = Path("tests/nlu_corpus.jsonl")
REPORT_PATH = Path("docs/eval/nlu_corpus.md")
CACHE_PATH = "eval/nlu_corpus_cache.db"
AUDIT_PATH = Path("eval/results/nlu_corpus_audit.db")
# One line at a time: free-tier per-key limits (Groq 8K tokens/min, about 7 NLU
# calls a minute) are the bottleneck, and parallel lines only turn into 429s
# and fail-overs to slower models. Raise it only with a multi-key pool.
DEFAULT_CONCURRENCY = 1
RETRY_BACKOFF_S = 30.0
# Optional floor on seconds per live line. With one Groq key (8K tokens/min,
# about 1.3K tokens per NLU call) about 11 s keeps every line on the primary
# model instead of letting the 6 s NLU timeout fail it over to another model,
# which would mix models between runs that are meant to be compared.
REF = date(2026, 4, 1)
# Report rows in reading order; ``Notes`` always goes last. A label not listed
# here goes right after the last row with the same BEFORE/AFTER pair key
# (``_pair_key``), else at the end of the rows, so a new AFTER_Pnn lands next
# to its BEFORE_Pnn instead of after Notes (Phase 38).
SECTION_ORDER = (
    "BEFORE", "AFTER", "DEFAULT_EFFORT", "LOW_EFFORT",
    "FILLER_BEFORE", "FILLER_VETO",
    "BEFORE_P32", "AFTER_P32", "HELDOUT_BEFORE_P32", "HELDOUT_AFTER_P32",
    "REGEX_ONLY_P32", "HELDOUT_REGEX_ONLY_P32",
    "CLAUDE_P37", "CLAUDE_P37_NO_GUARD", "CLAUDE_P37_PROBES",
    "AMOUNTS_P39",
)
NOTES_SECTION = "Notes"

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
    """Term labels: field values, ask %, cents clarify, dollar-total ask, ambiguous amount."""
    out: dict[str, Any] = dict(line.get("terms", {}))
    if line.get("ask_pct") is not None:
        out["settlement_ask_pct"] = float(line["ask_pct"])
    if line.get("cents_ambiguity") is not None:
        out["cents_ambiguity"] = int(line["cents_ambiguity"])
    if line.get("ask_total_cents") is not None:
        out["ask_total_cents"] = int(line["ask_total_cents"])
    if line.get("amount_ambiguous") is not None:
        out["amount_ambiguous"] = int(line["amount_ambiguous"])
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
    if out.settlement_ask_total_cents is not None:
        terms["ask_total_cents"] = out.settlement_ask_total_cents
    if out.amount_ambiguous_cents is not None:
        terms["amount_ambiguous"] = out.amount_ambiguous_cents
    return terms


def has_terms(out: VerifiedAnalysis) -> bool:
    """Same ``has_terms`` that ``post_verify`` hands to ``repair_stance``."""
    return (
        bool(out.terms)
        or out.settlement_ask_pct is not None
        or out.cents_ambiguity_bare is not None
        or out.tiers_ambiguous
        or out.settlement_ask_total_cents is not None
        or out.amount_ambiguous_cents is not None
    )


def stance_rule(utterance: str, *, terms: bool) -> str:
    """Which ``repair_stance`` rule decides this line (mirrors its order).

    ``injection`` (only changes an LLM accept), ``reject_phrase``,
    ``accept_phrase`` (un-negated), ``short_ack`` (no terms), else ``none``.
    A rule can fire and still agree with the LLM; compare raw vs repaired.
    """
    if _INJECTION_RE.search(utterance):
        return "injection"
    if _has_unguarded_reject_phrase(utterance):
        return "reject_phrase"
    if _has_unguarded_accept_phrase(utterance):
        return "accept_phrase"
    if not terms and _ack_dominant(utterance):
        return "short_ack"
    return "none"


def raw_stance_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deep copies with ``predicted.stance`` set to the raw LLM stance (guards off)."""
    out = copy.deepcopy(records)
    for r in out:
        if r.get("skipped"):
            continue
        if "stance_raw" not in r["predicted"]:
            raise ValueError(f"record {r['id']} has no predicted.stance_raw (pre-Phase 37 run)")
        r["predicted"]["stance"] = r["predicted"]["stance_raw"]
    return out


class _NluReplyRecorder:
    """Proxy over the LLM client that keeps each task's last NLU reply text.

    ``analyze`` may call twice (JSON retry); the last reply is the one it used.
    Everything else is forwarded to the wrapped client unchanged.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last: dict[int, str] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def chat_text(self, role: Any, messages: Any, max_tokens: int, **kw: Any) -> str:
        text = await self._inner.chat_text(role, messages, max_tokens, **kw)
        if role == "nlu":
            self.last[id(asyncio.current_task())] = text
        return text


def _raw_stance(reply: str) -> str:
    """Stance the LLM reply carries before repair; unparseable ⇒ ``other`` (as analyze)."""
    try:
        return _parse_analysis(reply).stance
    except Exception:  # noqa: BLE001 - analyze falls back to an empty analysis
        return "other"


def _raw_firm(reply: str) -> bool:
    """``firm`` flag the LLM reply carries before ``repair_firm``; unparseable ⇒ False."""
    try:
        return bool(_parse_analysis(reply).firm)
    except Exception:  # noqa: BLE001 - analyze falls back to an empty analysis
        return False


def rescore_guards(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deep copies with stance (and firm, when ``firm_raw`` is saved) re-repaired by current code.

    Replays ``repair_stance(stance_raw, …)`` and ``repair_firm(firm_raw, …)`` so a
    guard change is measured on a saved paid run without new LLM calls (Phase 45).
    ``has_terms`` comes from the record when saved, else from the predicted terms
    (which miss only ``tiers_ambiguous``). Fast-path lines are left as they are.
    """
    out = copy.deepcopy(records)
    for r in out:
        if r.get("skipped") or r.get("stance_source") == "fast_path":
            continue
        pred = r["predicted"]
        if "stance_raw" not in pred:
            raise ValueError(f"record {r['id']} has no predicted.stance_raw (pre-Phase 37 run)")
        terms = r.get("has_terms", bool(pred.get("terms")))
        pred["stance"] = repair_stance(pred["stance_raw"], r["text"], has_terms=terms)
        r["stance_rule"] = stance_rule(r["text"], terms=terms)
        if "firm_raw" in pred:
            pred["firm"] = repair_firm(pred["firm_raw"], r["text"])
    return out


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
        f"- git: `{meta['git']}`  profile=`{meta['profile']}`  ref={REF.isoformat()}"
        + (f"  providers=`{meta['providers']}`" if meta.get("providers") else ""),
        *(
            [f"- stance: {meta['stance_variant']}"] if meta.get("stance_variant") else []
        ),
        f"- lines: {summary['n']}  skipped (LLM unavailable): {summary['skipped']}",
        "- model share: "
        + (", ".join(f"{m}={c}" for m, c in meta["models"].most_common()) or "none"),
        *(
            ["- mixed models: allowed (`--allow-mixed-models`); not a one-model row"]
            if meta.get("mixed") else []
        ),
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

One row is one model: the runner exits 2 without writing a row when a
fail-over or retry let a second model answer (`--allow-mixed-models` writes it
anyway and marks it). A Groq `gpt-oss-120b` row needs at least two Groq keys
from separate organizations (`GROQ_API_KEY_1`, `_2`, ...): one full corpus
pass is about 230K tokens (about 1.3K per NLU call), above one free-tier
organization's 200K tokens/day, so one key runs dry near line 157 (Phase 32)
and the rest of the run would fail over to another model.

Paid targets (Phase 41): the shipped `demo` NLU route starts with Claude Sonnet
5.5 marked `budgeted: true`. Without `--providers`, the runner drops budgeted
targets and prints one line saying so, so a default run stays on the free
chain and spends nothing. `--allow-budgeted` keeps them (and the run then
counts against the demo's daily budget in the app DB). Approved paid runs use
an explicit `--providers` file, which is used as is (e.g. the Phase 37 and
Phase 40 files).
"""


def _pair_key(label: str) -> str:
    """``HELDOUT_AFTER_P32`` → ``HELDOUT_P32``: the label with its BEFORE/AFTER token removed."""
    return re.sub(r"(?:^|_)(?:BEFORE|AFTER)(?=_|$)", "", label).strip("_")


def section_order(labels: list[str]) -> list[str]:
    """Known rows in ``SECTION_ORDER``, others next to their pair (file order), Notes last."""
    order = [s for s in SECTION_ORDER if s in labels]
    for s in labels:
        if s in SECTION_ORDER or s == NOTES_SECTION:
            continue
        same = [i for i, o in enumerate(order) if _pair_key(o) == _pair_key(s)]
        order.insert(same[-1] + 1 if same else len(order), s)
    return order + ([NOTES_SECTION] if NOTES_SECTION in labels else [])


def write_report(path: Path, label: str, section: str) -> None:
    """Replace (or add) the ``## label`` section; rows ordered by ``section_order``."""
    sections: dict[str, str] = {}
    if path.exists():
        parts = re.split(r"(?m)^(?=## )", path.read_text())
        for part in parts[1:]:
            sections[part.split("\n", 1)[0][3:].strip()] = part.rstrip() + "\n"
    sections[label] = section
    order = section_order(list(sections))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER + "\n" + "\n".join(sections[s] for s in order))


async def run_corpus(
    corpus: list[dict[str, Any]],
    *,
    profile: str,
    concurrency: int = DEFAULT_CONCURRENCY,
    audit: AuditLog | None = None,
    llm: Any | None = None,
    retries: int = 1,
    retry_backoff_s: float = RETRY_BACKOFF_S,
    min_interval_s: float = 0.0,
    providers_path: Path | None = None,
    allow_budgeted: bool = False,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Analyze every line; returns per-line records and answering-model counts.

    Skipped lines (any exception from ``analyze``) are re-run up to ``retries``
    times, each pass after ``retry_backoff_s`` seconds; a record that still
    failed keeps ``skipped=True`` and ``error``. A line that made an uncached
    LLM call holds its slot for at least ``min_interval_s`` (rate pacing).
    With ``audit``, each line's LLM calls are audited under ``corpus:<id>``.
    ``llm`` overrides the client built from ``profile`` (tests);
    ``providers_path`` replaces ``config/providers.yaml`` for the built client;
    without it, ``budgeted`` (paid) targets are dropped unless ``allow_budgeted``.
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
    # Every model that returned a reply for the line (error attempts excluded).
    line_answered: dict[str, set[str]] = {}
    current: dict[int, str] = {}
    live_lines: set[str] = set()

    def on_call(meta: dict[str, Any]) -> None:
        tid = id(asyncio.current_task())
        if meta.get("model") and tid in current:
            line_models[current[tid]] = f"{meta.get('provider')}/{meta['model']}"
            if not meta.get("error"):
                line_answered.setdefault(current[tid], set()).add(line_models[current[tid]])
            if not meta.get("cache_hit"):
                live_lines.add(current[tid])

    hook = audit_llm_calls(audit, then=on_call) if audit is not None else on_call
    owns_llm = llm is None
    if llm is None and providers_path is None and not allow_budgeted:
        # The client reads the YAML only while it is built, so a temp copy will do.
        with tempfile.TemporaryDirectory() as tmp:
            path, removed = without_budgeted(_DEFAULT_PROVIDERS, Path(tmp))
            mine = [r for r in removed if r.startswith(f"{profile}/")]
            if mine:
                print(f"skipped budgeted (paid) targets: {', '.join(mine)} "
                      "(--allow-budgeted keeps)")
            llm = make_client(settings, on_call=hook, providers_path=path)
    elif llm is None:
        llm = make_client(settings, on_call=hook, providers_path=providers_path)
    else:
        llm.on_call = hook
    recorder = _NluReplyRecorder(llm)
    sem = asyncio.Semaphore(concurrency)

    async def one(line: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            started = asyncio.get_running_loop().time()
            try:
                return await _analyze_line(line)
            finally:
                if line["id"] in live_lines:
                    live_lines.discard(line["id"])
                    left = min_interval_s - (asyncio.get_running_loop().time() - started)
                    if left > 0:
                        await asyncio.sleep(left)

    async def _analyze_line(line: dict[str, Any]) -> dict[str, Any]:
        tid = id(asyncio.current_task())
        current[tid] = line["id"]
        recorder.last.pop(tid, None)
        line_answered.pop(line["id"], None)  # a retry pass starts clean
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
                    llm=recorder, settings=settings, ref=REF,
                )
        except Exception as e:  # noqa: BLE001 - any failure is a skip, retried below
            rec["skipped"] = True
            kind = "" if isinstance(e, LLMUnavailable) else f"{type(e).__name__}: "
            rec["error"] = (kind + str(e))[:200]
            return rec
        reply = recorder.last.pop(tid, None)
        if reply is None:  # fast path: no LLM call, no stance repair
            raw, rule, source = out.stance, "fast_path", "fast_path"
            firm_raw = out.firm
        else:
            raw, source = _raw_stance(reply), "llm"
            firm_raw = _raw_firm(reply)
            rule = stance_rule(line["text"], terms=has_terms(out))
            # Guard against this mirror drifting from the shipped repair.
            if repair_stance(raw, line["text"], has_terms=has_terms(out)) != out.stance:
                raise RuntimeError(f"{line['id']}: raw stance does not replay to the repaired one")
        rec["predicted"] = {
            **predicted_flags(out, settings.hostility_threshold),
            "stance": out.stance,
            "stance_raw": raw,
            "firm_raw": firm_raw,
            "terms": predicted_terms(out),
        }
        rec["has_terms"] = has_terms(out)
        rec["stance_rule"] = rule
        rec["stance_source"] = source
        rec["model"] = line_models.get(line["id"], "fast_path")
        rec["answered_by"] = sorted(line_answered.get(line["id"], ()))
        return rec

    try:
        records = list(await asyncio.gather(*(one(ln) for ln in corpus)))
        by_id = {ln["id"]: ln for ln in corpus}
        for _ in range(retries):
            todo = [i for i, r in enumerate(records) if r.get("skipped")]
            if not todo:
                break
            print(
                f"nlu_corpus: retrying {len(todo)} skipped line(s) after "
                f"{retry_backoff_s:g}s",
                file=sys.stderr,
            )
            await asyncio.sleep(retry_backoff_s)
            redo = await asyncio.gather(*(one(by_id[records[i]["id"]]) for i in todo))
            for i, r in zip(todo, redo, strict=True):
                records[i] = r
    finally:
        aclose = getattr(llm, "aclose", None)
        if owns_llm and aclose is not None:
            await aclose()
    for r in records:
        if not r.get("skipped"):
            models[r["model"]] += 1
    return records, models


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def unanswered(records: list[dict[str, Any]]) -> list[str]:
    """Ids of lines with no prediction (still skipped after the retry pass)."""
    return [r["id"] for r in records if r.get("skipped")]


def mixed_models(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{model: line ids}`` when more than one model answered the run, else ``{}``.

    Uses each record's ``answered_by`` (Phase 38+), else its last ``model``
    (older records). Skipped and fast-path lines are ignored.
    """
    by_model: dict[str, list[str]] = {}
    for r in records:
        if r.get("skipped"):
            continue
        for m in r.get("answered_by", [r.get("model")]):
            if m and m != "fast_path":
                by_model.setdefault(m, []).append(r["id"])
    return dict(sorted(by_model.items())) if len(by_model) > 1 else {}


def main(argv: list[str] | None = None) -> int:
    """CLI entry: run, score, write report section + per-line JSONL."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True, help="report section, e.g. BEFORE / AFTER")
    ap.add_argument("--profile", default="demo")
    ap.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--retry-backoff-s", type=float, default=RETRY_BACKOFF_S)
    ap.add_argument(
        "--min-interval-s", type=float, default=0.0,
        help="minimum seconds per uncached line (pace a single free-tier key)",
    )
    ap.add_argument("--audit-db", type=Path, default=AUDIT_PATH)
    ap.add_argument(
        "--providers", type=Path, default=None,
        help="providers.yaml for this run (default config/providers.yaml)",
    )
    ap.add_argument(
        "--allow-budgeted", action="store_true",
        help="keep paid `budgeted: true` targets of the default providers file",
    )
    ap.add_argument(
        "--no-repair-stance", action="store_true",
        help="score the raw LLM stance (repair_stance off); eval-only, app/ unchanged",
    )
    ap.add_argument(
        "--from-records", type=Path, default=None,
        help="rescore a saved nlu_corpus_<label>.jsonl instead of calling the LLM",
    )
    ap.add_argument(
        "--rescore-guards", action="store_true",
        help=(
            "with --from-records: re-run repair_stance / repair_firm (current code) "
            "on the saved raw LLM stance and firm flag; no LLM call"
        ),
    )
    ap.add_argument(
        "--allow-mixed-models", action="store_true",
        help="write the row even if more than one provider/model answered (noted in it)",
    )
    args = ap.parse_args(argv)

    if args.from_records is not None:
        records = load_corpus(args.from_records)
        models: Counter[str] = Counter(
            r["model"] for r in records if not r.get("skipped")
        )
        corpus = records
    else:
        corpus = load_corpus(args.corpus)
        args.audit_db.parent.mkdir(parents=True, exist_ok=True)
        audit = AuditLog(args.audit_db)
        try:
            records, models = asyncio.run(
                run_corpus(
                    corpus,
                    profile=args.profile,
                    concurrency=args.concurrency,
                    audit=audit,
                    retry_backoff_s=args.retry_backoff_s,
                    min_interval_s=args.min_interval_s,
                    providers_path=args.providers,
                    allow_budgeted=args.allow_budgeted,
                )
            )
        finally:
            audit.close()
    missing = unanswered(records)
    if missing:
        partial = args.audit_db.parent / f"nlu_corpus_{args.label.lower()}.partial.jsonl"
        partial.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
        print(
            f"nlu_corpus FAILED: {len(corpus) - len(missing)}/{len(corpus)} lines answered "
            f"after retry; no report written. Missing: {', '.join(missing)}. "
            f"Records: {partial}. Rerun to fill gaps from the response cache.",
            file=sys.stderr,
        )
        return 2
    mixed = mixed_models(records)
    if mixed and not args.allow_mixed_models:
        if args.from_records is None:
            partial = args.audit_db.parent / f"nlu_corpus_{args.label.lower()}.partial.jsonl"
            partial.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
        print(
            f"nlu_corpus FAILED: {len(mixed)} models answered one run (fail-over or retry "
            "on another model); no report written. "
            + "; ".join(f"{m}: {', '.join(ids)}" for m, ids in mixed.items())
            + ". Fix the route or key pool and rerun, or pass --allow-mixed-models.",
            file=sys.stderr,
        )
        return 2
    if args.rescore_guards:
        if args.from_records is None:
            ap.error("--rescore-guards needs --from-records")
        records = rescore_guards(records)
    if args.no_repair_stance:
        records = raw_stance_records(records)
    summary = score(records)
    meta = {
        "git": _git_sha(),
        "profile": args.profile,
        "models": models,
        "providers": str(args.providers) if args.providers else None,
        "stance_variant": (
            "raw LLM (repair_stance off, eval-only)" if args.no_repair_stance
            else "repaired (repair_stance on, as shipped)"
        ),
    }
    if mixed:
        meta["mixed"] = True
    if args.from_records is not None:
        meta["stance_variant"] += f"; rescored from `{args.from_records}`"
    if args.rescore_guards:
        meta["stance_variant"] += "; stance/firm guards re-run with current code"
    write_report(REPORT_PATH, args.label, render_section(args.label, summary, meta))
    raw = REPORT_PATH.parent / f"nlu_corpus_{args.label.lower()}.jsonl"
    raw.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
    print(render_section(args.label, summary, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
