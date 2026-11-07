"""Scripted-call runner (Phase 71a): oracle scripts pass, and the acts path runs end to end.

- Every script except ``tiers_suggested_lines`` meets its expected moves on the
  oracle path; that one fails on "the first two payments" exactly as the flat
  rule does (``tiers_ambiguous``), which is what the acts path fixes.
- With ``NLU_SCHEMA=acts`` and a fake model that returns the right acts for each
  line, the suggested-lines tiers call and the 73-vs-75 call meet every move,
  and a phase-69 script is skipped with a note.
"""

from __future__ import annotations

import json
from typing import Any

from eval.scripted_calls.run import all_scripts, run_script, scripted_settings
from tests.wsutil import offline_settings


async def test_oracle_scripts_meet_their_moves() -> None:
    settings = scripted_settings("oracle", offline_settings())
    for script in all_scripts():
        res = await run_script(script, settings=settings)
        if res.skipped:  # a script whose requirement is not on this branch
            assert script.get("requires") and not res.turns
            continue
        bad = [t["i"] for t in res.turns if not t["ok"]]
        assert bad == ([2, 4] if script["name"] == "tiers_suggested_lines" else []), script["name"]


class _ActsByLine:
    """Fake ``nlu`` model: the acts reply for each rep line, keyed by the line."""

    def __init__(self, replies: dict[str, list[dict[str, Any]]]) -> None:
        self.replies = replies
        self.on_call = None
        self.seen: list[str] = []

    async def chat_text(self, role: str, messages: list[dict], max_tokens: int, **_: Any) -> str:
        line = messages[-1]["content"].rsplit("Rep: ", 1)[1]
        self.seen.append(messages[-1]["content"])
        return json.dumps({"acts": self.replies.get(line, [])})


def _item(q: str) -> dict[str, Any]:
    return {"type": "rule_item", "quote": q}


async def test_acts_path_runs_the_tier_and_price_calls() -> None:
    llm = _ActsByLine(
        {
            "Up to eight payments, at least $190 each, and the schedule is flexible.": [
                {"type": "state_rule", "field": "max_payments", "value": 8,
                 "quote": "eight payments"},
                {"type": "state_rule", "field": "min_payment_cents", "value": 19000,
                 "quote": "$190"},
                {"type": "state_rule", "field": "payment_structure", "value": "flexible",
                 "quote": "schedule is flexible"},
            ],
            "There are three payment tiers.": [
                {"type": "announce_rules", "field": "min_payment_tiers", "count": 3,
                 "quote": "three payment tiers"}
            ],
            "The first two payments must be at least one hundred ninety dollars.": [
                _item("The first two payments must be at least one hundred ninety dollars")
            ],
            "Payments three to five must be at least two hundred fifty dollars.": [
                _item("Payments three to five must be at least two hundred fifty dollars")
            ],
            "Payments six to eight must be at least three hundred dollars.": [
                _item("Payments six to eight must be at least three hundred dollars")
            ],
            "That's all the tiers.": [{"type": "list_done", "quote": "That's all the tiers"}],
            "Up to eight payments, at least $100 each, even payments.": [
                {"type": "state_rule", "field": "max_payments", "value": 8,
                 "quote": "eight payments"},
                {"type": "state_rule", "field": "min_payment_cents", "value": 10000,
                 "quote": "$100"},
                {"type": "state_rule", "field": "payment_structure", "value": "even",
                 "quote": "even payments"},
            ],
            "seventy five percent": [
                {"type": "price_ask", "pct": 75, "firmness": "opening",
                 "quote": "seventy five percent"}
            ],
            "I can't go lower than seventy five": [
                {"type": "price_ask", "pct": 75, "firmness": "final",
                 "quote": "I can't go lower than seventy five"}
            ],
            "I think seventy three is the lowest I can go": [
                {"type": "price_ask", "pct": 73, "firmness": "final",
                 "quote": "seventy three is the lowest I can go"}
            ],
        }
    )
    settings = scripted_settings("acts", offline_settings(llm_profile="offline"))
    for name in ("tiers_suggested_lines", "price_73_vs_75"):
        res = await run_script(all_scripts(name)[0], settings=settings, llm=llm, nlu="acts")
        assert [t["i"] for t in res.turns if not t["ok"]] == [], name
    # The context told the model a tier list was open while the tiers came in.
    assert any('"collecting"' in s for s in llm.seen)
