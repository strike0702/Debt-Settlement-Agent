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


async def test_min_interval_paces_lines_with_llm_calls(monkeypatch) -> None:
    import asyncio

    import eval.nlu_corpus as nc

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        await real_sleep(0)

    monkeypatch.setattr(nc.asyncio, "sleep", fake_sleep)
    lines = _lines(2)
    records, _ = await nc.run_corpus(
        lines, profile="offline", llm=_flaky_llm({}), retry_backoff_s=0, min_interval_s=5.0
    )
    assert nc.unanswered(records) == []
    assert len(slept) == 2 and all(4.0 < s <= 5.0 for s in slept)


# --- Phase 37: raw vs repaired stance from one pass; --providers; rescore ---


def _stance_llm(replies: dict[str, str]):
    """FakeLLM whose NLU reply stance is picked by a substring of the rep line."""
    import json

    from app.llm.client import FakeLLM

    class ByText(FakeLLM):
        async def chat_text(self, role, messages, max_tokens, *, json_mode=False):
            user = messages[-1]["content"]
            stance = next((s for k, s in replies.items() if k in user), "info")
            self.enqueue(role, json.dumps({"terms": [], "stance": stance}))
            return await super().chat_text(role, messages, max_tokens, json_mode=json_mode)

    return ByText()


_GUARD_LINES = [
    # Accept phrase in a question: the guard forces accept over the LLM's question.
    {"id": "g01", "tags": [], "text": "Is that agreed?", "stance": "question"},
    # Reject phrase under negation: the guard forces reject over the LLM's accept.
    {"id": "g02", "tags": [], "text": "That's not too low.", "agent": "counter",
     "stance": "accept"},
    # No rule fires: raw and repaired agree.
    {"id": "g03", "tags": [], "text": "Let me look into the file.", "stance": "stall"},
    # Short ack: the guard forces accept over the LLM's info.
    {"id": "g04", "tags": [], "text": "Okay, yeah.", "stance": "accept"},
]
_GUARD_REPLIES = {
    "Is that agreed": "question",
    "not too low": "accept",
    "look into": "stall",
    "Okay, yeah": "info",
}


async def test_records_keep_raw_and_repaired_stance() -> None:
    from eval.nlu_corpus import run_corpus

    records, _ = await run_corpus(
        _GUARD_LINES, profile="offline", llm=_stance_llm(_GUARD_REPLIES), retry_backoff_s=0
    )
    by = {r["id"]: r for r in records}
    assert (by["g01"]["predicted"]["stance_raw"], by["g01"]["predicted"]["stance"]) == (
        "question", "accept")
    assert by["g01"]["stance_rule"] == "accept_phrase"
    assert (by["g02"]["predicted"]["stance_raw"], by["g02"]["predicted"]["stance"]) == (
        "accept", "reject")
    assert by["g02"]["stance_rule"] == "reject_phrase"
    assert by["g03"]["predicted"]["stance_raw"] == by["g03"]["predicted"]["stance"] == "stall"
    assert by["g03"]["stance_rule"] == "none"
    assert by["g04"]["predicted"]["stance_raw"] == "info"
    assert by["g04"]["stance_rule"] == "short_ack"
    assert all(r["stance_source"] == "llm" for r in records)


async def test_fast_path_line_has_raw_equal_to_repaired() -> None:
    from eval.nlu_corpus import run_corpus

    line = {"id": "m01", "tags": [], "agent": "min_ask", "text": "Two hundred fifty dollars.",
            "stance": "info", "terms": {"min_payment_cents": 25000}}
    records, _ = await run_corpus([line], profile="offline", llm=_stance_llm({}),
                                  retry_backoff_s=0)
    r = records[0]
    assert r["stance_source"] == "fast_path" and r["stance_rule"] == "fast_path"
    assert r["predicted"]["stance_raw"] == r["predicted"]["stance"]


async def test_unparseable_reply_counts_as_raw_other() -> None:
    from app.llm.client import FakeLLM
    from eval.nlu_corpus import run_corpus

    class Junk(FakeLLM):
        async def chat_text(self, role, messages, max_tokens, *, json_mode=False):
            return "not json"

    line = {"id": "j01", "tags": [], "text": "Sounds good to me.", "stance": "accept"}
    records, _ = await run_corpus([line], profile="offline", llm=Junk(), retry_backoff_s=0)
    assert records[0]["predicted"]["stance_raw"] == "other"
    assert records[0]["predicted"]["stance"] == "accept"  # the phrase rule still fires


async def test_raw_stance_scoring_differs_only_in_stance() -> None:
    from eval.nlu_corpus import raw_stance_records, run_corpus

    records, _ = await run_corpus(
        _GUARD_LINES, profile="offline", llm=_stance_llm(_GUARD_REPLIES), retry_backoff_s=0
    )
    raw = raw_stance_records(records)
    guarded, unguarded = score(records), score(raw)
    assert guarded["stance"]["accept"]["fp_ids"] == ["g01"]
    assert unguarded["stance"]["accept"]["fp_ids"] == []
    assert guarded["stance"]["reject"]["fp_ids"] == ["g02"]
    assert unguarded["stance"]["reject"]["fp_ids"] == []
    assert unguarded["stance"]["accept"]["fn_ids"] == ["g04"]
    assert guarded["flags"] == unguarded["flags"] and guarded["terms"] == unguarded["terms"]
    # The input records are not mutated by the raw view.
    assert records[0]["predicted"]["stance"] == "accept"


def test_raw_stance_records_require_raw_field() -> None:
    import pytest

    from eval.nlu_corpus import raw_stance_records

    rec = {"id": "x", "tags": [], "expected": {"stance": "info"},
           "predicted": {"stance": "info"}}
    with pytest.raises(ValueError, match="stance_raw"):
        raw_stance_records([rec])


def test_cli_providers_flag_reaches_client(tmp_path: Path, monkeypatch) -> None:
    import json

    import eval.nlu_corpus as nc

    corpus = tmp_path / "c.jsonl"
    corpus.write_text("".join(json.dumps(ln) + "\n" for ln in _lines(1)))
    monkeypatch.setattr(nc, "REPORT_PATH", tmp_path / "report.md")
    seen: dict = {}
    real = nc.run_corpus

    async def fake_run(corpus, **kw):
        seen.update(kw)
        return await real(corpus, **{**kw, "llm": _flaky_llm({})})

    monkeypatch.setattr(nc, "run_corpus", fake_run)
    prov = tmp_path / "p.yaml"
    prov.write_text("profiles: {}\nproviders: {}\n")
    rc = nc.main(["--label", "T", "--profile", "offline", "--corpus", str(corpus),
                  "--providers", str(prov), "--audit-db", str(tmp_path / "a.db")])
    assert rc == 0
    assert seen["providers_path"] == prov
    assert f"providers=`{prov}`" in (tmp_path / "report.md").read_text()


def test_run_corpus_passes_providers_path_to_make_client(monkeypatch, tmp_path) -> None:
    import asyncio

    import eval.nlu_corpus as nc

    seen: dict = {}

    def fake_make_client(settings, **kw):
        seen.update(kw)
        return _flaky_llm({})

    monkeypatch.setattr(nc, "make_client", fake_make_client)
    prov = tmp_path / "p.yaml"
    records, _ = asyncio.run(nc.run_corpus(_lines(1), profile="offline", providers_path=prov,
                                           retry_backoff_s=0))
    assert seen["providers_path"] == prov
    assert nc.unanswered(records) == []


def test_cli_no_repair_stance_rescores_saved_records(tmp_path: Path, monkeypatch) -> None:
    """--from-records + --no-repair-stance: second variant from the same replies, no LLM."""
    import asyncio
    import json

    import eval.nlu_corpus as nc

    records, _ = asyncio.run(nc.run_corpus(
        _GUARD_LINES, profile="offline", llm=_stance_llm(_GUARD_REPLIES), retry_backoff_s=0
    ))
    saved = tmp_path / "saved.jsonl"
    saved.write_text("".join(json.dumps(r) + "\n" for r in records))
    report = tmp_path / "report.md"
    monkeypatch.setattr(nc, "REPORT_PATH", report)

    async def no_llm(*a, **kw):
        raise AssertionError("rescoring must not call the LLM")

    monkeypatch.setattr(nc, "run_corpus", no_llm)
    rc = nc.main(["--label", "RAW", "--from-records", str(saved), "--no-repair-stance"])
    assert rc == 0
    text = report.read_text()
    assert "stance: raw LLM" in text
    out = [json.loads(ln) for ln in (tmp_path / "nlu_corpus_raw.jsonl").read_text().splitlines()]
    assert [r["predicted"]["stance"] for r in out] == ["question", "accept", "stall", "info"]


# --- Phase 38 (item 32.3): one labelled row = one answering model ---


def _routed_llm(plan: dict[str, list[tuple[str, str | None]]]):
    """FakeLLM whose NLU call per line emits ``(provider/model, error)`` metas, then answers.

    ``plan`` maps a text key to the metas of that line's attempts, e.g. a fail-over
    ``[("groq/a", "timeout"), ("gemini/b", None)]``. Unlisted lines answer as ``groq/a``.
    """
    from app.llm.client import FakeLLM

    class Routed(FakeLLM):
        async def chat_text(self, role, messages, max_tokens, *, json_mode=False):
            user = messages[-1]["content"]
            attempts = next((v for k, v in plan.items() if k in user), [("groq/a", None)])
            for spec, err in attempts:
                provider, model = spec.split("/", 1)
                await self._emit({"role": role, "provider": provider, "model": model,
                                  "cache_hit": False, "error": err})
            return _OK

    return Routed()


async def test_single_model_run_is_not_mixed() -> None:
    from eval.nlu_corpus import mixed_models, run_corpus

    records, _ = await run_corpus(_lines(3), profile="offline", llm=_routed_llm({}))
    assert mixed_models(records) == {}
    assert all(r["answered_by"] == ["groq/a"] for r in records)


async def test_failover_to_another_model_marks_the_run_mixed() -> None:
    """A timed-out primary that fails over answers from a second model: two models, one row."""
    from eval.nlu_corpus import mixed_models, run_corpus

    llm = _routed_llm({"Line number 1 ": [("groq/a", "timeout after 6s"), ("gemini/b", None)]})
    records, _ = await run_corpus(_lines(3), profile="offline", llm=llm)
    assert records[1]["answered_by"] == ["gemini/b"]  # the failed attempt did not answer
    assert mixed_models(records) == {"gemini/b": ["z01"], "groq/a": ["z00", "z02"]}


async def test_two_models_inside_one_line_is_mixed() -> None:
    """The JSON retry inside ``analyze`` can land on a second model; that line is mixed too."""
    from eval.nlu_corpus import mixed_models, run_corpus

    llm = _routed_llm({"Line number 0 ": [("groq/a", None), ("gemini/b", None)]})
    records, _ = await run_corpus(_lines(1), profile="offline", llm=llm)
    assert mixed_models(records) == {"gemini/b": ["z00"], "groq/a": ["z00"]}


def test_old_records_fall_back_to_model_and_ignore_fast_path() -> None:
    from eval.nlu_corpus import mixed_models

    old = [
        {"id": "a", "model": "groq/a"},
        {"id": "b", "model": "fast_path"},
        {"id": "c", "skipped": True},
    ]
    assert mixed_models(old) == {}
    assert mixed_models([*old, {"id": "d", "model": "gemini/b"}]) == {
        "gemini/b": ["d"], "groq/a": ["a"]
    }


def _cli(nc, tmp_path: Path, monkeypatch, llm, *extra: str) -> int:
    import json

    corpus = tmp_path / "c.jsonl"
    corpus.write_text("".join(json.dumps(ln) + "\n" for ln in _lines(3)))
    monkeypatch.setattr(nc, "REPORT_PATH", tmp_path / "report.md")
    real = nc.run_corpus

    async def fake_run(corpus, **kw):
        return await real(corpus, **{**kw, "llm": llm})

    monkeypatch.setattr(nc, "run_corpus", fake_run)
    return nc.main([
        "--label", "T", "--profile", "offline", "--corpus", str(corpus),
        "--retry-backoff-s", "0", "--audit-db", str(tmp_path / "a.db"), *extra,
    ])


def test_cli_fails_loudly_on_mixed_models_by_default(tmp_path: Path, monkeypatch, capsys) -> None:
    import eval.nlu_corpus as nc

    llm = _routed_llm({"Line number 1 ": [("groq/a", "timeout"), ("gemini/b", None)]})
    rc = _cli(nc, tmp_path, monkeypatch, llm)
    err = capsys.readouterr().err
    assert rc == 2
    assert "FAILED: 2 models answered one run" in err
    assert "gemini/b: z01" in err and "groq/a: z00, z02" in err
    assert not (tmp_path / "report.md").exists()
    assert (tmp_path / "nlu_corpus_t.partial.jsonl").exists()


def test_cli_allow_mixed_models_writes_the_row_and_says_so(tmp_path: Path, monkeypatch) -> None:
    import eval.nlu_corpus as nc

    llm = _routed_llm({"Line number 1 ": [("groq/a", "timeout"), ("gemini/b", None)]})
    rc = _cli(nc, tmp_path, monkeypatch, llm, "--allow-mixed-models")
    assert rc == 0
    text = (tmp_path / "report.md").read_text()
    assert "mixed models: allowed" in text and "groq/a=2, gemini/b=1" in text


def test_cli_from_records_also_refuses_mixed_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    import eval.nlu_corpus as nc

    saved = tmp_path / "saved.jsonl"
    rows = [
        {"id": "a", "tags": [], "model": m,
         "expected": {**{f: False for f in FLAGS}, "stance": "info", "terms": {}},
         "predicted": {**{f: False for f in FLAGS}, "stance": "info", "stance_raw": "info",
                       "terms": {}}}
        for m in ("groq/a", "gemini/b")
    ]
    saved.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(nc, "REPORT_PATH", tmp_path / "report.md")
    rc = nc.main(["--label", "R", "--from-records", str(saved),
                  "--audit-db", str(tmp_path / "a.db")])
    assert rc == 2 and "2 models answered" in capsys.readouterr().err
    assert nc.main(["--label", "R", "--from-records", str(saved), "--allow-mixed-models",
                    "--audit-db", str(tmp_path / "a.db")]) == 0


# --- Phase 38 (item 32.5): rows stay next to their BEFORE/AFTER pair, Notes last ---


def _headings(p: Path) -> list[str]:
    return [ln[3:] for ln in p.read_text().splitlines() if ln.startswith("## ")]


def test_new_after_row_lands_next_to_its_before_row(tmp_path: Path) -> None:
    p = tmp_path / "r.md"
    for label in ("BEFORE", "AFTER", "Notes", "BEFORE_P40", "OTHER_P40", "AFTER_P40"):
        write_report(p, label, f"## {label}\n\nx\n")
    assert _headings(p) == ["BEFORE", "AFTER", "BEFORE_P40", "AFTER_P40", "OTHER_P40", "Notes"]


def test_heldout_pair_groups_by_its_own_prefix(tmp_path: Path) -> None:
    p = tmp_path / "r.md"
    for label in ("Notes", "HELDOUT_BEFORE_P41", "BEFORE_P41", "AFTER_P41", "HELDOUT_AFTER_P41"):
        write_report(p, label, f"## {label}\n\nx\n")
    assert _headings(p) == [
        "HELDOUT_BEFORE_P41", "HELDOUT_AFTER_P41", "BEFORE_P41", "AFTER_P41", "Notes"
    ]


def test_shipped_report_rows_are_in_section_order() -> None:
    """docs/eval/nlu_corpus.md: every P32 AFTER row sits right after its BEFORE row."""
    from eval.nlu_corpus import REPORT_PATH

    h = _headings(REPORT_PATH)
    assert h[-1] == "Notes"
    for before, after in [("BEFORE", "AFTER"), ("BEFORE_P32", "AFTER_P32"),
                          ("HELDOUT_BEFORE_P32", "HELDOUT_AFTER_P32"),
                          ("FILLER_BEFORE", "FILLER_VETO")]:
        assert h.index(after) == h.index(before) + 1, (before, after, h)
