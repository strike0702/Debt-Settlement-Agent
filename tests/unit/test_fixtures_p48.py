"""Phase 48: curated scenario fixtures and the optional haggle style in ``sim.json``.

- ``haggling_rep`` autoplays the whole price ladder in order: first offer,
  hold, small step, the rep's firm floor, final offer, accept on repeat.
- Fixtures without ``"haggle"`` keep the easy rep, so they play as before.
- Card descriptions are plain words (no policy jargon), and every rep card
  whose ``sim.json`` has a floor shows it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.autoplay import load_autoplay_scenario, new_autoplay_call, parse_haggle, run_autoplay
from app.config import Settings
from app.domain.scenario import (
    SCENARIOS_ROOT,
    load_scenario,
    rep_account_from_card,
    resolve_scenario_dir,
)
from sim.haggle import EASY, Haggle
from tests.wsutil import make_client, offline_settings

_IDS = sorted(p.parent.name for p in SCENARIOS_ROOT.glob("*/sim.json"))


async def _ladder(
    scenario_id: str, settings: Settings
) -> tuple[list[tuple[str, str | None]], object]:
    call = load_scenario(resolve_scenario_dir(scenario_id), rebase_to=date(2026, 10, 9))
    orch, creditor = new_autoplay_call(call, scenario_id, settings=settings, audit=None)
    moves: list[tuple[str, str | None]] = []

    async def on_agent(utt) -> None:  # type: ignore[no-untyped-def]
        if utt.action.intent.value in ("COUNTER", "CONFIRM_SCHEDULE", "ESCALATE"):
            reason = utt.action.reason
            moves.append(
                (
                    utt.action.intent.value,
                    "anchor" if reason and reason.startswith("bp=") and not moves else reason,
                )
            )

    async def on_creditor(_text: str) -> None:
        return None

    result = await run_autoplay(orch, creditor, on_agent=on_agent, on_creditor=on_creditor)
    return moves, (result, orch.session.agreement)


@pytest.mark.parametrize("max_counters", [4, 6])
async def test_haggling_rep_plays_the_whole_ladder(max_counters: int) -> None:
    moves, (result, agreement) = await _ladder(
        "haggling_rep", offline_settings(max_counters=max_counters)
    )
    assert moves[:5] == [
        ("COUNTER", "anchor"),
        ("COUNTER", "hold"),
        ("COUNTER", "step"),
        ("COUNTER", "final_counter"),
        ("CONFIRM_SCHEDULE", "rep_firm"),
    ]
    assert result.outcome == "deal"  # type: ignore[attr-defined]
    assert agreement is not None and agreement.bp == 5000  # type: ignore[attr-defined]


def test_haggling_rep_meta_matches_the_autoplay_outcome(tmp_path: Path) -> None:
    from app.domain.scenario import list_scenario_metas

    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/p48-haggle") as ws:
        ws.send_json(
            {
                "type": "start",
                "scenario_id": "haggling_rep",
                "autoplay": True,
                "autoplay_pause_ms": 0,
            }
        )
        frames: list[dict] = []
        while not frames or frames[-1]["type"] != "autoplay_done":
            frames.append(ws.receive_json())
    meta = {m.id: m for m in list_scenario_metas()}["haggling_rep"]
    assert meta.expected == frames[-1]["outcome"] == "deal"


def test_sim_json_haggle_is_optional_and_parsed() -> None:
    assert parse_haggle(None) == EASY
    assert parse_haggle({"style": "holder", "hold_turns": 2, "steps_bp": [1000]}) == Haggle(
        style="holder", hold_turns=2, steps_bp=(1000,)
    )
    assert (
        parse_haggle({"style": "staller", "stall_on": "counter", "steps_bp": []}).stall_on
        == "counter"
    )
    for bad in (
        {"style": "angry"},
        {"hold_turns": 1},
        {"style": "holder", "steps_bp": [0]},
        {"style": "staller", "stall_on": "x"},
        "holder",
    ):
        with pytest.raises(ValueError):
            parse_haggle(bad)


@pytest.mark.parametrize("scenario_id", [i for i in _IDS if i != "haggling_rep"])
def test_older_fixtures_keep_the_easy_rep(scenario_id: str) -> None:
    folder = SCENARIOS_ROOT / scenario_id
    assert "haggle" not in json.loads((folder / "sim.json").read_text())
    call = load_scenario(folder, rebase_to=date(2026, 10, 9))
    assert load_autoplay_scenario(scenario_id, call).haggle == EASY


_JARGON = (
    "WRAP",
    "->",
    "→",
    "escalat",
    "reaches",
    "feasib",
    "guardrail",
    "bp",
    "NO_DEAL",
    "ladder",
)


@pytest.mark.parametrize("scenario_id", _IDS)
def test_card_descriptions_are_plain_words(scenario_id: str) -> None:
    meta = json.loads((SCENARIOS_ROOT / scenario_id / "meta.json").read_text())
    text = meta["description"]
    assert text and text[0].isupper() and text.endswith(".")
    assert not [j for j in _JARGON if j in text], text
    assert len(text) <= 90  # three lines on a card


@pytest.mark.parametrize("scenario_id", _IDS)
def test_every_rep_card_shows_the_sim_floor(scenario_id: str) -> None:
    """[47.2] A human rep sees the same floor the simulated rep holds."""
    folder = SCENARIOS_ROOT / scenario_id
    acct = rep_account_from_card((folder / "rep_card.md").read_text(encoding="utf-8"))
    sim = json.loads((folder / "sim.json").read_text())
    assert acct["rules"]["floor_bp"] == sim["floor_bp"]
