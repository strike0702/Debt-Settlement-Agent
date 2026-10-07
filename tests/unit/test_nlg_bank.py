"""NLG template bank (Phase 21): guard-clean entries, deterministic pick, no LLM."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from app.agent.guards import template_guard
from app.agent.nlg import TEMPLATE_ONLY_INTENTS, TEMPLATES, speak_action
from app.agent.nlg_bank import (
    DEFAULT_BANK_PATH,
    bank_key,
    load_bank,
    parse_bank,
    pick_template,
)
from app.config import Settings
from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact
from app.llm.client import FakeLLM
from tests.seed7 import slot

REF = date(2026, 4, 1)


def _bank_json() -> dict:
    return json.loads(DEFAULT_BANK_PATH.read_text())


def test_every_bank_entry_passes_template_guard() -> None:
    data = _bank_json()
    assert data["entries"], "bank is empty"
    for entry in data["entries"]:
        # H3 act keys (Phase 24b) are not intents: ``ACK`` / ``ANSWER:<topic>``.
        if entry["intent"] != "ACK" and not entry["intent"].startswith("ANSWER:"):
            assert Intent(entry["intent"]) not in TEMPLATE_ONLY_INTENTS
        allowed, required = set(entry["placeholders"]), set(entry["required"])
        assert required <= allowed
        assert entry["templates"], entry
        for t in entry["templates"]:
            res = template_guard(t, allowed, required)
            assert res.ok, (entry["intent"], t, res.reason, res.offending)


def test_bank_never_speaks_offer_total_as_one_payment() -> None:
    for entry in _bank_json()["entries"]:
        for t in entry["templates"]:
            assert "payments of {offer_total}" not in t
            assert "installments of {offer_total}" not in t


def _counter() -> Action:
    return Action(
        intent=Intent.COUNTER,
        facts={
            "counter_pct": Fact(
                id="counter_pct", kind="pct", value=4200, visibility="PUBLIC", source="engine"
            ),
            "offer_total": Fact(
                id="offer_total", kind="money", value=52500, visibility="PUBLIC", source="engine"
            ),
        },
        required={"counter_pct", "offer_total"},
        next_phase=Phase.NEGOTIATE,
    )


def test_pick_is_deterministic_and_from_bank() -> None:
    bank = load_bank()
    action = _counter()
    pool = bank[bank_key("COUNTER", ["counter_pct", "offer_total"])]
    a = pick_template(action, call_id="call-x", turn=3, bank=bank)
    b = pick_template(action, call_id="call-x", turn=3, bank=bank)
    assert a == b and a in pool
    picks = {pick_template(action, call_id="call-x", turn=t, bank=bank) for t in range(30)}
    assert len(picks) > 1, "turn varies the pick"


def test_pick_skips_entries_failing_live_guard_and_misses_unknown_key() -> None:
    bank = parse_bank(
        {
            "entries": [
                {
                    "intent": "COUNTER",
                    "placeholders": ["counter_pct", "offer_total"],
                    "required": ["counter_pct", "offer_total"],
                    "templates": ["Would {counter_pct} work?"],  # misses offer_total
                }
            ]
        }
    )
    assert pick_template(_counter(), call_id="c", turn=1, bank=bank) is None
    other = Action(intent=Intent.REFUSE_COMMIT, next_phase=Phase.NEGOTIATE)
    assert pick_template(other, call_id="c", turn=1, bank=bank) is None


@pytest.mark.asyncio
async def test_bank_mode_speak_action_makes_no_llm_call() -> None:
    fake = FakeLLM()  # empty queue: any call would be recorded and raise
    settings = Settings(nlg_mode="bank", llm_profile="offline")
    out = await speak_action(_counter(), REF, llm=fake, settings=settings, call_id="c", turn=2)
    assert fake.calls == []
    pool = load_bank()[bank_key("COUNTER", ["counter_pct", "offer_total"])]
    spoken = " ".join(out)
    assert "42%" in spoken and "$525" in spoken
    assert spoken != TEMPLATES[Intent.COUNTER]
    assert any(t.split("{")[0].strip() and spoken.startswith(t.split("{")[0]) for t in pool)


@pytest.mark.asyncio
async def test_bank_mode_falls_back_to_templates_when_key_missing(tmp_path: Path) -> None:
    empty = tmp_path / "bank.json"
    empty.write_text('{"entries": []}')
    settings = Settings(nlg_mode="bank", llm_profile="offline", nlg_bank_path=str(empty))
    out = await speak_action(_counter(), REF, llm=None, settings=settings, call_id="c", turn=1)
    assert out[0].startswith("We can propose 42% of the balance")


@pytest.mark.asyncio
async def test_bank_mode_full_call_zero_llm_calls(tmp_path: Path) -> None:
    """A whole offline call (oracle NLU, bank NLG) never touches the LLM."""
    from eval.run_eval import _build_settings, run_one_scenario

    settings = _build_settings(profile="offline", nlg="template", nlu="oracle").model_copy(
        update={"nlg_mode": "bank"}
    )
    fake = FakeLLM()
    result = await run_one_scenario(
        slot(0), settings=settings, llm=fake, sim_phrasing="template", audit_dir=tmp_path
    )
    assert fake.calls == []
    assert result["agent_lines"]
    bank_texts = {t.split("{")[0] for ts in load_bank().values() for t in ts}
    assert any(
        any(prefix and line.startswith(prefix) for prefix in bank_texts)
        for line in result["agent_lines"]
    ), "at least one line came from the bank"


# Phase 33: the bank is the demo voice, so its copy must sound spoken, not form-like.
_STIFF = re.compile(r"\b(kindly|tentative|validate|acknowledge|equals|set at)\b", re.IGNORECASE)


def test_bank_has_no_stiff_phrasing() -> None:
    stiff = [
        (e["intent"], t)
        for e in _bank_json()["entries"]
        for t in e["templates"]
        if _STIFF.search(t)
    ]
    assert stiff == []


def test_read_back_entries_are_confirmation_questions() -> None:
    entries = [e for e in _bank_json()["entries"] if e["intent"] == "READ_BACK"]
    assert entries
    for entry in entries:
        assert len(entry["templates"]) == 8, "same variant count as Phase 21"
        for t in entry["templates"]:
            assert t.rstrip().endswith("?"), t
            assert t.startswith(("Just to confirm", "So ", "And ", "Let me", "I have", "Okay")), t
