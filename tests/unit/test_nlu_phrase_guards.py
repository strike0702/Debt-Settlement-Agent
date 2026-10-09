"""Phase 45 (D / D2): accept, reject and firm phrases inside a question, after a
conditional or after a negator never force a label; the LLM's reading stands.

Probe lines come from ``docs/eval/nlu_guard_20261008/probes.jsonl`` (ledger
31.1 / 31.2); firm lines from ``tests/nlu_corpus_firm.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.nlu import repair_firm, repair_stance

ROOT = Path(__file__).resolve().parents[2]
PROBES = {
    r["id"]: r
    for r in (
        json.loads(ln)
        for ln in (ROOT / "docs/eval/nlu_guard_20261008/probes.jsonl").read_text().splitlines()
        if ln.strip()
    )
}
FIRM_LINES = [
    json.loads(ln)
    for ln in (ROOT / "tests/nlu_corpus_firm.jsonl").read_text().splitlines()
    if ln.strip()
]

# Probes whose phrase is guarded: the LLM's stance passes through unchanged.
GUARDED = ("q01", "q02", "q03", "q04", "q05", "q06", "q07", "q09")


@pytest.mark.parametrize("pid", GUARDED)
@pytest.mark.parametrize("llm", ["stall", "question", "other", "info"])
def test_guarded_probe_keeps_llm_stance(pid: str, llm: str) -> None:
    assert repair_stance(llm, PROBES[pid]["text"]) == llm


@pytest.mark.parametrize(
    ("pid", "llm", "want"),
    [
        # Negated reject phrase, then a plain accept phrase in the next clause.
        ("q08", "reject", "accept"),
        # Reject phrase inside a question, then a plain "we accept".
        ("q10", "other", "accept"),
        # Controls: plain phrases still force their label.
        ("q11", "accept", "reject"),
        ("q12", "other", "accept"),
    ],
)
def test_unguarded_probe_phrases_still_decide(pid: str, llm: str, want: str) -> None:
    assert repair_stance(llm, PROBES[pid]["text"]) == want
    assert PROBES[pid]["stance"] == want


@pytest.mark.parametrize(
    "text",
    [
        "Agreed.",
        "That works, nothing else to add.",
        "No problem, that works.",
        "Yes, we accept.",
    ],
)
def test_plain_accept_phrases_still_accept(text: str) -> None:
    assert repair_stance("other", text) == "accept"


@pytest.mark.parametrize(
    "text",
    [
        "That's too low.",
        "No that's too low",
        "No, that doesn't work for us.",
        "We cannot go lower than that.",
    ],
)
def test_plain_reject_phrases_still_reject(text: str) -> None:
    assert repair_stance("accept", text) == "reject"


@pytest.mark.parametrize("line", FIRM_LINES, ids=lambda r: r["id"])
def test_firm_corpus_phrase_list_never_adds_a_false_firm(line: dict) -> None:
    # Near-misses: the phrase list alone never sets firm. Firm lines rely on the
    # LLM flag (the phrase list does not try to cover every synonym).
    if not line.get("firm"):
        assert repair_firm(False, line["text"]) is False
    assert repair_firm(True, line["text"]) is True


@pytest.mark.parametrize(
    "text",
    [
        "Sixty percent is our floor.",
        "That's our final offer.",
        "We can't go below our floor.",
        "We won't go any lower.",
        "Fifty-five is the lowest we can go",
        "That's non-negotiable.",
    ],
)
def test_plain_firm_phrases_set_firm(text: str) -> None:
    assert repair_firm(False, text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Is that your final offer?",
        "Is that your final offer",
        "If sixty were our floor, would that help?",
        "That isn't our final number.",
        "There's no bottom line yet.",
    ],
)
def test_guarded_firm_phrases_do_not_set_firm(text: str) -> None:
    assert repair_firm(False, text) is False
