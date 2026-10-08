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


# --- Phase 32 (item 28.4): retry skipped lines once, fail loudly on gaps ---

_OK = '{"terms": [], "stance": "info"}'


def _lines(n: int) -> list[dict]:
    return [
        {"id": f"z{i:02d}", "tags": [], "text": f"Line number {i} about the account.",
         "stance": "info"}
        for i in range(n)
    ]


def _flaky_llm(fail_first: dict[str, int]):
    """FakeLLM whose NLU call fails the first N times for lines containing a key."""
    from app.llm.client import FakeLLM, LLMUnavailable

    class Flaky(FakeLLM):
        async def chat_text(self, role, messages, max_tokens, *, json_mode=False):
            user = messages[-1]["content"]
            for key, left in fail_first.items():
                if key in user and left > 0:
                    fail_first[key] = left - 1
                    raise LLMUnavailable(f"429 for {key}")
            self.enqueue(role, _OK)
            return await super().chat_text(role, messages, max_tokens, json_mode=json_mode)

    return Flaky()


def test_runner_defaults_to_one_line_at_a_time() -> None:
    import inspect

    from eval.nlu_corpus import DEFAULT_CONCURRENCY, run_corpus

    assert DEFAULT_CONCURRENCY == 1
    assert inspect.signature(run_corpus).parameters["concurrency"].default == 1


async def test_skipped_line_is_retried_once_and_recovers() -> None:
    from eval.nlu_corpus import run_corpus, unanswered

    llm = _flaky_llm({"Line number 1 ": 1})
    records, models = await run_corpus(
        _lines(3), profile="offline", llm=llm, retry_backoff_s=0
    )
    assert unanswered(records) == []
    assert [r["id"] for r in records] == ["z00", "z01", "z02"]
    assert sum(models.values()) == 3


async def test_line_failing_twice_stays_skipped() -> None:
    from eval.nlu_corpus import run_corpus, unanswered

    llm = _flaky_llm({"Line number 2 ": 2})
    records, _ = await run_corpus(_lines(3), profile="offline", llm=llm, retry_backoff_s=0)
    assert unanswered(records) == ["z02"]
    assert "429" in records[2]["error"]


async def test_non_llm_error_is_a_skip_not_a_crash() -> None:
    from app.llm.client import FakeLLM
    from eval.nlu_corpus import run_corpus, unanswered

    class Boom(FakeLLM):
        async def chat_text(self, role, messages, max_tokens, *, json_mode=False):
            raise RuntimeError("provider exploded")

    records, _ = await run_corpus(_lines(2), profile="offline", llm=Boom(), retry_backoff_s=0)
    assert unanswered(records) == ["z00", "z01"]
    assert records[0]["error"].startswith("RuntimeError")


def test_cli_fails_loudly_when_lines_unanswered(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    import eval.nlu_corpus as nc

    corpus = tmp_path / "c.jsonl"
    corpus.write_text("".join(json.dumps(ln) + "\n" for ln in _lines(2)))
    report = tmp_path / "report.md"
    monkeypatch.setattr(nc, "REPORT_PATH", report)
    monkeypatch.setattr(nc, "CACHE_PATH", str(tmp_path / "cache.db"))
    # The offline profile's FakeLLM has an empty queue: every call fails.
    rc = nc.main([
        "--label", "T", "--profile", "offline", "--corpus", str(corpus),
        "--retry-backoff-s", "0", "--audit-db", str(tmp_path / "a.db"),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "FAILED: 0/2 lines answered" in err and "z00, z01" in err
    assert not report.exists()
    assert (tmp_path / "nlu_corpus_t.partial.jsonl").exists()


def test_cli_writes_report_when_all_answered(tmp_path: Path, monkeypatch) -> None:
    import json

    import eval.nlu_corpus as nc

    corpus = tmp_path / "c.jsonl"
    corpus.write_text("".join(json.dumps(ln) + "\n" for ln in _lines(2)))
    report = tmp_path / "report.md"
    monkeypatch.setattr(nc, "REPORT_PATH", report)
    real = nc.run_corpus

    async def fake_run(corpus, **kw):
        return await real(corpus, **{**kw, "llm": _flaky_llm({})})

    monkeypatch.setattr(nc, "run_corpus", fake_run)
    rc = nc.main([
        "--label", "T", "--profile", "offline", "--corpus", str(corpus),
        "--retry-backoff-s", "0", "--audit-db", str(tmp_path / "a.db"),
    ])
    assert rc == 0
    assert "lines: 2  skipped (LLM unavailable): 0" in report.read_text()
    assert (tmp_path / "nlu_corpus_t.jsonl").exists()


def test_heldout_private_set_schema() -> None:
    """Phase 32 held-out set: written before the private-info fix, never tuned on."""
    held = load_corpus(Path("tests/nlu_corpus_heldout.jsonl"))
    main_ids = {ln["id"] for ln in load_corpus()}
    assert len({ln["id"] for ln in held}) == len(held)
    assert not main_ids & {ln["id"] for ln in held}
    pos = [ln for ln in held if ln.get("asks_client_private_info")]
    neg = [ln for ln in held if "private_neg" in ln["tags"]]
    assert len(pos) >= 15 and len(neg) >= 15
    for ln in held:
        assert "heldout" in ln["tags"], ln["id"]
        assert ln["stance"] in _STANCES, ln["id"]
        assert set(ln) <= {"id", "tags", "text", "stance", "terms", "ask_pct", *FLAGS}
