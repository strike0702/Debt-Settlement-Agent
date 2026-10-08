"""Phase 37: guard report scoring on hand-built records (no LLM)."""

from __future__ import annotations

from eval.nlu_guard_report import (
    changed_lines,
    context_rows,
    filler_false_accepts,
    per_class,
    render,
    rule_tally,
)


def _r(rid: str, text: str, exp: str, raw: str, rep: str, rule: str, tags=()) -> dict:
    return {"id": rid, "text": text, "tags": list(tags), "stance_rule": rule,
            "expected": {"stance": exp},
            "predicted": {"stance": rep, "stance_raw": raw}}


RECS = [
    _r("a", "Is that agreed?", "question", "question", "accept", "accept_phrase"),
    _r("b", "Okay, yeah.", "accept", "info", "accept", "short_ack"),
    _r("c", "That's not too low.", "accept", "other", "reject", "reject_phrase"),
    _r("d", "Let me look.", "stall", "stall", "stall", "none"),
    _r("e", "sure ok", "info", "accept", "accept", "short_ack", tags=["filler"]),
]


def test_changed_lines_verdicts() -> None:
    got = {c["id"]: c["verdict"] for c in changed_lines(RECS)}
    assert got == {"a": "hurt", "b": "helped", "c": "neutral"}


def test_per_class_on_and_off() -> None:
    on, off = per_class(RECS, "stance"), per_class(RECS, "stance_raw")
    assert (on["accept"]["tp"], on["accept"]["fp"], on["accept"]["fn"]) == (1, 2, 1)
    assert (off["accept"]["tp"], off["accept"]["fp"], off["accept"]["fn"]) == (0, 1, 2)
    assert off["question"]["f1"] == 1.0 and on["question"]["recall"] == 0.0


def test_rule_tally_and_filler() -> None:
    t = rule_tally(RECS)
    assert t["short_ack"]["fired"] == 2 and t["short_ack"]["helped"] == 1
    assert t["accept_phrase"]["hurt"] == 1 and t["reject_phrase"]["neutral"] == 1
    assert filler_false_accepts(RECS, "stance") == filler_false_accepts(RECS, "stance_raw") == ["e"]


def test_context_rows_list_forced_lines_only() -> None:
    old = [
        {"id": "x", "text": "That's too low.", "expected": {"stance": "reject"},
         "predicted": {"stance": "reject"}},
        {"id": "y", "text": "Is that agreed?", "expected": {"stance": "question"},
         "predicted": {"stance": "accept"}},
        {"id": "z", "text": "Let me look.", "expected": {"stance": "stall"},
         "predicted": {"stance": "stall"}},
    ]
    rows = {r["id"]: r for r in context_rows(old)}
    assert set(rows) == {"x", "y"}
    assert rows["x"]["correct"] and not rows["y"]["correct"]


def test_render_has_tables() -> None:
    md = render(RECS)
    assert "| stance | pos | P on |" in md and "| a | Is that agreed? |" in md
