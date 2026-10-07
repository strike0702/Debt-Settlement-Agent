"""Rep-card suggested replies must not quote the agent's own figure (Phase 33).

A reply like "Thirty-two is too low" goes stale the moment policy changes the
agent's counter. These tests run the curated easy_deal call offline (autoplay:
oracle NLU, template NLG, sim creditor) and check no suggested reply names a
percentage the agent itself said, and that rejection lines carry no figure.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from app.autoplay import new_autoplay_call, run_autoplay
from app.domain.actions import Intent
from app.domain.scenario import load_scenario, rep_card_suggestions, resolve_scenario_dir
from tests.wsutil import offline_settings

SCENARIOS_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "scenarios"

_ONES = "one two three four five six seven eight nine".split()
_TEENS = "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_NUMBER_WORD = re.compile(
    r"\b(" + "|".join(_ONES + _TEENS + _TENS + ["hundred"]) + r")\b", re.IGNORECASE
)


def _pct_word(pct: int) -> str:
    """Whole percent 1..99 as the rep card spells it ("thirty-one")."""
    if pct < 10:
        return _ONES[pct - 1]
    if pct < 20:
        return _TEENS[pct - 10]
    tens, ones = divmod(pct, 10)
    return _TENS[tens - 2] + ("" if ones == 0 else "-" + _ONES[ones - 1])


def _suggestions(scenario_id: str) -> list[str]:
    md = (resolve_scenario_dir(scenario_id) / "rep_card.md").read_text(encoding="utf-8")
    return rep_card_suggestions(md)


def test_pct_word() -> None:
    assert [_pct_word(p) for p in (5, 13, 30, 31, 42)] == [
        "five", "thirteen", "thirty", "thirty-one", "forty-two",
    ]  # fmt: skip


async def test_easy_deal_replies_never_quote_the_agents_counter() -> None:
    call = load_scenario(resolve_scenario_dir("easy_deal"), rebase_to=date(2026, 10, 7))
    orch, creditor = new_autoplay_call(call, "easy_deal", settings=offline_settings(), audit=None)
    agent_bp: set[int] = set()

    async def on_agent(utt) -> None:  # noqa: ANN001
        fact = utt.action.facts.get("counter_pct")
        if utt.action.intent == Intent.COUNTER and fact is not None:
            agent_bp.add(int(fact.value))

    async def on_creditor(_text: str) -> None:
        return None

    await run_autoplay(orch, creditor, on_agent=on_agent, on_creditor=on_creditor)
    assert agent_bp, "the agent countered at least once"
    words = {_pct_word(bp // 100) for bp in agent_bp if bp % 100 == 0}
    assert words, agent_bp
    for line in _suggestions("easy_deal"):
        for w in words:
            assert not re.search(rf"\b{w}\b", line, re.IGNORECASE), (w, line)


def test_rejection_replies_name_no_figure_before_too_low() -> None:
    """A "too low" line rejects without restating the agent's number (every card)."""
    cards = sorted(SCENARIOS_ROOT.glob("*/rep_card.md"))
    assert cards
    for card in cards:
        for line in rep_card_suggestions(card.read_text(encoding="utf-8")):
            head, sep, _ = line.lower().partition("too low")
            if sep:
                assert not _NUMBER_WORD.search(head), (card.parent.name, line)
