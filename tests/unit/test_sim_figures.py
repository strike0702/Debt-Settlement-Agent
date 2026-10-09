"""Phase 46a: the sim's LLM rewrite keeps the draft's figures and stance, else falls back.

Evidence lines are from the live A/B (``docs/eval/ab_20261007``: s0007_015,
s0007_004, pair 17, pair 3); the drafts are made up so that each rewrite is
what a free model produced from a correct code-built line.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact
from app.domain.nlu_types import TurnAnalysis
from app.llm.client import FakeLLM
from sim.creditor import CreditorPolicy, CreditorReply
from sim.figures import figures, rewrite_problem
from sim.scenarios import generate_one

# (draft, oracle stance of the draft, LLM rewrite, expected fallback reason)
EVIDENCE: list[tuple[str, str, str, str]] = [
    # s0007_015: invented percentages, amounts and dates.
    ("We are looking for a 65% settlement.", "offer", "We want 65% of the 10% balance.", "figures"),
    (
        "The first payment is due March 31.",
        "info",
        "The first payment is the $346.80 due March 31.",
        "figures",
    ),
    (
        "That is too low. We could come down to 60%.",
        "reject",
        "That's too low, and we cannot go below 21%.",
        "figures",
    ),
    # s0007_004: self-contradiction with the same figure.
    (
        "That is too low. We could come down to 48%.",
        "reject",
        "We cannot accept 48%, but we will accept 48%.",
        "stance",
    ),
    (
        "That schedule does not work for us — our minimum is actually $150.",
        "reject",
        "That schedule doesn't work, but yes, it is agreed. Our minimum is $150.",
        "stance",
    ),
    # Pair 17: "settle the 100% balance".
    ("We can accept 48%.", "accept", "We can settle the 100% balance at 48%.", "figures"),
    # Pair 3: client-private wording with a made-up figure.
    ("Yes, that's right.", "confirm", "Yes, that's right, with a monthly income of 0.", "figures"),
    ("Yes, that's right.", "confirm", "Yes, that's right, given the client's income.", "private"),
]


@pytest.mark.parametrize(("draft", "stance", "rewrite", "reason"), EVIDENCE)
def test_evidence_rewrites_fall_back(draft: str, stance: str, rewrite: str, reason: str) -> None:
    assert rewrite_problem(draft, rewrite, stance=stance) == reason


@pytest.mark.parametrize(
    ("draft", "stance", "rewrite"),
    [
        ("That is too low. We could come down to 60%.", "reject", "Too low for us. Sixty percent."),
        ("We can accept 48%.", "accept", "OK, 48% works for us."),
        ("Yes, that payment schedule works for us. Agreed.", "accept", "No problem, that works."),
        ("Our minimum payment is $150.", "info", "Each payment must be at least 150 dollars."),
        ("The maximum is 6 payments.", "info", "That one's easy: up to six payments."),
        ("The first payment is due March 31.", "info", "First payment lands on March 31st."),
        ("No, that's not correct.", "deny", "No, that's wrong."),
    ],
)
def test_faithful_rewrites_are_kept(draft: str, stance: str, rewrite: str) -> None:
    assert rewrite_problem(draft, rewrite, stance=stance) is None


def test_figures_normalises_units() -> None:
    assert figures("Up to 6 payments of $346.80, first due March 31, 2027, at 48%.") == {
        ("num", 6),
        ("money", 34680),
        ("date", (3, 31)),
        ("pct", 4800),
    }
    assert figures("six payments of three hundred forty-six dollars, forty-eight percent") == {
        ("num", 6),
        ("money", 34600),
        ("pct", 4800),
    }
    # Pronoun "one" and style ordinals are not figures.
    assert figures("That one works for the first payment.") == set()
    assert rewrite_problem("x", "  ", stance=None) == "empty"


def _counter(bp: int) -> Action:
    fact = Fact(id="counter_pct", kind="pct", value=bp, visibility="PUBLIC", source="engine")
    return Action(
        intent=Intent.COUNTER,
        facts={"counter_pct": fact},
        required={"counter_pct"},
        next_phase=Phase.NEGOTIATE,
        reason=f"bp={bp}",
    )


_REJECT_48 = TurnAnalysis(stance="reject", settlement_ask_pct=48.0, ask_quote="48%")


class _BoomLLM(FakeLLM):
    async def chat_text(self, role: Any, messages: Any, max_tokens: int, **kw: Any) -> str:
        raise RuntimeError("provider exploded")


async def test_phrase_falls_back_and_counts_reasons() -> None:
    sc = generate_one("flexible", "deal", seed=101)
    llm = FakeLLM()
    rep = CreditorPolicy(sc, phrasing="llm", llm=llm)
    draft = CreditorReply(text="That is too low. We could come down to 48%.", analysis=_REJECT_48)
    llm.enqueue("sim", "We cannot accept 48%, but we will accept 48%.")
    assert (await rep._phrase(draft, "")).text == draft.text
    llm.enqueue("sim", "Too low. We could do 40%.")
    assert (await rep._phrase(draft, "")).text == draft.text
    llm.enqueue("sim", "I need you to commit to 48% today.")
    assert (await rep._phrase(draft, "")).text == draft.text
    llm.enqueue("sim", "Too low for us. We could do 48%.")
    assert (await rep._phrase(draft, "")).text == "Too low for us. We could do 48%."
    assert rep.rewrite_stats == {
        "attempts": 4,
        "fallbacks": {"stance": 1, "figures": 1, "commit": 1},
    }
    boom = CreditorPolicy(sc, phrasing="llm", llm=_BoomLLM())
    assert (await boom._phrase(draft, "")).text == draft.text
    assert boom.rewrite_stats == {"attempts": 1, "fallbacks": {"error": 1}}


async def test_template_phrasing_makes_no_attempts() -> None:
    rep = CreditorPolicy(generate_one("flexible", "deal", seed=101), phrasing="template")
    await rep.respond(_counter(1000))
    assert rep.rewrite_stats == {"attempts": 0, "fallbacks": {}}


# Phase 49 (ledger 46c.2 / 46c.5): Phase 46c re-check rewrites that slipped the
# stance check (s0007_040, s0007_019, s0007_028 / s0007_034, smoke hold line).
P49_STANCE: list[tuple[str, str, str]] = [
    ("We can go up to 3 payments.", "info", "We can go up to 3 payments. Actually, we can't."),
    (
        "The maximum is 6 payments.",
        "info",
        "Sure, the maximum is 6 payments. Actually, it isn't.",
    ),
    (
        "Actually, make that a maximum of 6 payments.",
        "info",
        "No, actually it's a maximum of 6 payments.",
    ),
    ("Up to 4 payments.", "info", "No, up to 4 payments."),
    ("We can't move on that yet.", "reject", "Sure, we can't move on that yet."),
    ("That is too low.", "reject", "Okay, that is too low."),
]


@pytest.mark.parametrize(("draft", "stance", "rewrite"), P49_STANCE)
def test_p49_self_contradictions_fall_back(draft: str, stance: str, rewrite: str) -> None:
    assert rewrite_problem(draft, rewrite, stance=stance) == "stance"


@pytest.mark.parametrize(
    ("draft", "stance", "rewrite"),
    [
        # A refusal may stack its "no"s; a no-problem idiom is not a leading "No".
        ("No, that is not right.", "deny", "No. That's not right."),
        ("We can't move on that yet.", "reject", "Sorry, we can't move on that yet."),
        ("We can do up to 10 payments.", "info", "No problem, we can do up to 10 payments."),
        # A "No" the draft already had, and a negation inside a longer sentence.
        ("No, it is 6 payments.", "info", "No, it's 6 payments."),
        ("We do not take tokens.", "info", "We don't take token payments, sorry."),
    ],
)
def test_p49_consistent_rewrites_are_kept(draft: str, stance: str, rewrite: str) -> None:
    assert rewrite_problem(draft, rewrite, stance=stance) is None
