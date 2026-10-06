"""Offline checks for the NLU corpus file and the eval scorer (no LLM)."""

from __future__ import annotations

from pathlib import Path

from app.agent.nlu import VerifiedAnalysis, VerifiedTerm
from app.domain.fields import FIELDS_BY_NAME
from eval.nlu_corpus import (
    AGENT_LINES,
    FLAGS,
    expected_flags,
    expected_terms,
    load_corpus,
    predicted_flags,
    predicted_terms,
    score,
    write_report,
)

_STANCES = {"offer", "counter", "accept", "reject", "stall", "info", "question", "other"}


def test_corpus_schema() -> None:
    corpus = load_corpus()
    assert len(corpus) >= 140
    assert len({ln["id"] for ln in corpus}) == len(corpus)
    allowed = {"id", "tags", "text", "agent", "pending", "stance", "terms", "ask_pct",
               "cents_ambiguity", *FLAGS}
    for ln in corpus:
        assert set(ln) <= allowed, ln["id"]
        assert ln["stance"] in _STANCES, ln["id"]
        assert ln.get("agent", "default") in AGENT_LINES, ln["id"]
        for field in ln.get("terms", {}):
            assert field in FIELDS_BY_NAME, ln["id"]
    tags = {t for ln in corpus for t in ln["tags"]}
    assert {"private", "private_neg", "injection", "hedge", "filler", "cents"} <= tags


def _rec(rid: str, tags: list[str], exp: dict, pred: dict) -> dict:
    return {"id": rid, "tags": tags, "expected": exp, "predicted": pred}


def test_score_precision_recall_and_filler() -> None:
    base = {f: False for f in FLAGS}
    recs = [
        _rec("a", [], {**base, "asks_client_private_info": True, "stance": "question",
                       "terms": {}},
             {**base, "asks_client_private_info": True, "stance": "question", "terms": {}}),
        _rec("b", ["filler"], {**base, "stance": "info", "terms": {"max_payments": 6}},
             {**base, "asks_client_private_info": True, "stance": "accept",
              "terms": {"max_payments": 6}}),
        _rec("c", [], {**base, "asks_client_private_info": True, "stance": "other",
                       "terms": {}},
             {**base, "stance": "other", "terms": {"max_payments": 1}}),
    ]
    s = score(recs)
    m = s["flags"]["asks_client_private_info"]
    assert (m["tp"], m["fp"], m["fn"]) == (1, 1, 1)
    assert m["precision"] == 0.5 and m["recall"] == 0.5
    assert s["filler"] == {"n": 1, "false_accept": 1, "ids": ["b"]}
    assert s["terms"]["miss_ids"] == ["c"]
    assert s["terms"]["exact_with_terms"] == 1.0


def test_label_and_prediction_shapes() -> None:
    line = {"hostility": 1.0, "ask_pct": 45, "terms": {"min_payment_cents": 25000},
            "cents_ambiguity": 110}
    assert expected_flags(line)["hostility"] is True
    assert expected_terms(line) == {
        "min_payment_cents": 25000, "settlement_ask_pct": 45.0, "cents_ambiguity": 110
    }
    out = VerifiedAnalysis(
        terms=[VerifiedTerm(field="max_payments", value=6, quote="six", verified=True)],
        settlement_ask_pct=45.0,
        hostility=0.7,
    )
    assert predicted_flags(out, 0.8)["hostility"] is False
    assert predicted_terms(out) == {"max_payments": 6, "settlement_ask_pct": 45.0}


def test_write_report_keeps_before_above_after(tmp_path: Path) -> None:
    p = tmp_path / "r.md"
    write_report(p, "AFTER", "## AFTER\n\nafter\n")
    write_report(p, "BEFORE", "## BEFORE\n\nbefore\n")
    write_report(p, "AFTER", "## AFTER\n\nafter2\n")
    text = p.read_text()
    assert text.index("## BEFORE") < text.index("## AFTER")
    assert "after2" in text and "\nafter\n" not in text


async def test_corpus_lines_are_audited_per_line(tmp_path: Path) -> None:
    """[20.4] each corpus line's LLM call is audited under ``corpus:<id>``."""
    from app.llm.client import FakeLLM
    from app.store.audit import AuditLog
    from eval.nlu_corpus import run_corpus

    corpus = [
        {"id": "z01", "tags": [], "text": "We need it settled by the end of the month.",
         "stance": "info"},
        {"id": "z02", "tags": [], "text": "Let me check with my supervisor first.",
         "stance": "stall"},
    ]
    fake = FakeLLM()
    for _ in corpus:
        fake.enqueue("nlu", '{"terms": [], "stance": "info"}')
    audit = AuditLog(tmp_path / "corpus_audit.db")
    records, _ = await run_corpus(corpus, profile="offline", audit=audit, llm=fake)
    assert not any(r.get("skipped") for r in records)
    for line in corpus:
        rows = audit.for_call(f"corpus:{line['id']}")
        assert [r["type"] for r in rows] == ["llm_call"]
        assert rows[0]["payload"]["role"] == "nlu"
