"""Operator-entered known creditor rules (Phase 69).

``start.known_rules`` is validated like the cushion (error frame, no call); the
session seeds the rules KNOWN from ``operator`` (audited, private); the agent does
not ask for them; a different value from the rep is CONTRADICTED; the rep stream
never carries them until the agent speaks them; ``firm.known_creditor_rules`` is
the per-creditor default that a call's entry replaces.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.session import CallSession
from app.domain.belief import TermStatus
from app.domain.known_rules import parse_known_rules
from app.domain.scenario import SCENARIO_TEMPLATE, load_scenario, scenario_payload_errors
from app.voice import ws as ws_mod
from tests.wsutil import _ACCEPT, _ASK, ack_all, make_client, turn

_SESSIONS: list[CallSession] = []
_RULES = {"max_payments": 8, "min_payment_cents": 10_000, "payment_structure": "even"}


class _RecordingSession(CallSession):
    def __post_init__(self) -> None:
        super().__post_init__()
        _SESSIONS.append(self)


@pytest.fixture(autouse=True)
def _record(monkeypatch: pytest.MonkeyPatch) -> None:
    _SESSIONS.clear()
    monkeypatch.setattr(ws_mod, "CallSession", _RecordingSession)


def _play(ws: Any, start: dict[str, Any], lines: list[dict[str, Any]]) -> list[dict]:
    ws.send_json({"type": "start", "scenario_id": "easy_deal", **start})
    frames = turn(ws)
    frames += ack_all(ws, frames)
    for msg in lines:
        ws.send_json(msg)
        batch = turn(ws)
        frames += batch + ack_all(ws, batch)
    return frames


@pytest.mark.parametrize(
    ("raw", "where"),
    [
        ({"min_payment_cents": 10_000,
          "min_payment_tiers": [{"from_payment": 3, "min_cents": 9_000}]},
         "more than the minimum payment"),
        ({"max_payments": 4, "min_payment_tiers": [{"from_payment": 6, "min_cents": 20_000}]},
         "after the last allowed payment"),
        ({"min_payment_tiers": [{"from_payment": 4, "min_cents": 30_000},
                                {"from_payment": 2, "min_cents": 40_000}]}, "later payment"),
        ({"max_payments": 4, "max_token_pays": 5}, "more than max payments"),
        ({"min_payment_cents": 99.5}, "whole cents"),
        ({"payment_structure": "weekly"}, "even, balloon or flexible"),
        ({"first_payment_date": "12/01/2026"}, "YYYY-MM-DD"),
        ({"first_payment_date": "2000-01-31"}, "before today's date"),
        ({"settlement_pct": 45}, "unknown rule"),
    ],
)
def test_invalid_rules_are_refused_with_a_visible_error(
    tmp_path: Path, raw: dict[str, Any], where: str
) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/k-bad") as ws:
        ws.send_json({"type": "start", "scenario_id": "easy_deal", "known_rules": raw})
        frame = ws.receive_json()
    assert frame["type"] == "error" and "known_rules" in frame["message"]
    assert where in frame["message"]
    assert _SESSIONS == []


def test_valid_rules_parse_to_belief_values() -> None:
    raw = {
        **_RULES,
        "min_payment_tiers": [{"from_payment": 3, "min_cents": 15_000}],
        "max_segments": 3,
        "max_token_pays": 2,
        "first_payment_date": "2030-01-31",
        "max_payments": None,  # null = not known
    }
    assert parse_known_rules(raw, as_of=date(2029, 12, 1)) == {
        "min_payment_cents": 10_000,
        "payment_structure": "even",
        "min_payment_tiers": [(3, 15_000)],
        "max_segments": 3,
        "max_token_pays": 2,
        "first_payment_date": date(2030, 1, 31),
    }


def test_entered_rules_are_known_from_operator_and_never_asked(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/k-op") as ws:
        frames = _play(ws, {"known_rules": _RULES}, [_ASK, _ACCEPT, _ACCEPT])
    belief = _SESSIONS[-1].belief
    for name, value in _RULES.items():
        term = belief.get(name)
        assert term.value == value and term.status == TermStatus.KNOWN
        assert term.evidence[0].source == "operator"
    # The token limit follows the entered max payments, still a default.
    assert belief.get("max_token_pays").value == 8
    seeds = [f for f in frames if f["type"] == "audit" and f["event"] == "seed_operator"]
    assert {r["payload"]["field"] for r in seeds} == set(_RULES)
    assert all(r["private"] for r in seeds)
    intents = [f["intent"] for f in frames if f["type"] == "phase" and f.get("intent")]
    assert "ASK" not in intents and "READ_BACK" not in intents
    assert "CONFIRM_SCHEDULE" in intents  # still read back at CONFIRM
    assert any(f["type"] == "agreement" for f in frames)


def test_rep_stating_a_different_value_contradicts(tmp_path: Path) -> None:
    six = {
        "type": "text",
        "text": "We can only do six payments.",
        "oracle": {
            "terms": [{"field": "max_payments", "value": 6, "quote": "six", "hedged": False}],
            "stance": "info",
        },
    }
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/k-x") as ws:
        _play(ws, {"known_rules": _RULES}, [six])
    term = _SESSIONS[-1].belief.get("max_payments")
    assert term.history == [8] and term.value == 6
    assert term.status == TermStatus.CONTRADICTED
    assert term.evidence[-1].source == "rep"


def test_rep_view_never_gets_the_entered_rules(tmp_path: Path) -> None:
    with make_client(tmp_path) as client, client.websocket_connect("/ws/call/k-rep?view=rep") as ws:
        frames = _play(ws, {"known_rules": _RULES}, [_ASK])
    assert _SESSIONS[-1].belief.get("max_payments").status == TermStatus.KNOWN
    for f in frames:
        if f["type"] == "belief":
            assert not {t["field"] for t in f["terms"]} & set(_RULES)
        assert not (f["type"] == "audit" and f["event"] == "seed_operator")


def test_firm_default_per_creditor_and_call_override(tmp_path: Path) -> None:
    payload = copy.deepcopy(SCENARIO_TEMPLATE)
    creditor = payload["offer"]["creditor"]
    payload["firm"]["known_creditor_rules"] = {creditor: {"max_payments": 6}, "Other Co": {}}
    assert scenario_payload_errors(payload) == []
    for start, expected in (({}, TermStatus.KNOWN), ({"known_rules": {}}, TermStatus.UNKNOWN)):
        with make_client(tmp_path) as client, client.websocket_connect("/ws/call/k-f") as ws:
            ws.send_json({"type": "start", "scenario_id": "custom", "scenario_payload": payload,
                          **start})
            ws.receive_json()
        assert _SESSIONS[-1].belief.get("max_payments").status == expected
    payload["firm"]["known_creditor_rules"] = {creditor: {"first_payment_date": "2030-01-31"}}
    assert [e["path"] for e in scenario_payload_errors(payload)] == ["firm.known_creditor_rules"]
    assert load_scenario(Path("fixtures/scenarios/easy_deal")).known_rules == {}
