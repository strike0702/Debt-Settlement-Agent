"""Phase 34: ``GET /scenarios/{id}/rep`` shows the rep their own account and rules.

The payload is parsed from ``rep_card.md`` only. These tests check that every
curated card parses and agrees with the scenario's ``offer.json`` / ``sim.json``,
that malformed or unknown rows are dropped rather than guessed, and that no
client or firm value reaches the rep (same scan as the rep-stream F7 tests).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.session import CallSession
from app.domain.scenario import (
    SCENARIO_TEMPLATE,
    SCENARIOS_ROOT,
    load_scenario,
    rep_account_from_card,
)
from tests.wsutil import leaked_private_values, make_client

CARD_IDS = sorted(p.parent.name for p in SCENARIOS_ROOT.glob("*/rep_card.md"))
_CLIENT_KEYS = ("client", "sda", "draft", "deposit", "withdraw", "fee", "ledger", "as_of")


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [k for k, v in value.items() for k in (k, *_keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in _keys(v)]
    return []


def _private(scenario_id: str) -> tuple[set[tuple[str, int | date]], date]:
    """Client blocklist, client dates, and firm fees for one curated scenario."""
    sc = load_scenario(SCENARIOS_ROOT / scenario_id, rebase_to=date.today())
    c = sc.client
    private = set(CallSession(sc).private_blocklist)
    private |= {("date", d) for d in (c.as_of_date, c.first_draft_date, c.last_draft_date)}
    private |= {("date", e.date) for e in c.ledger}
    fee_bp = round(sc.program_fee_pct * 10000)
    private |= {
        ("money", sc.bank_fee_cents),
        ("pct", fee_bp),
        ("money", round(sc.original_balance_cents * sc.program_fee_pct)),
        ("money", round(sc.creditor_balance_cents * sc.program_fee_pct)),
    }
    return {p for p in private if p[1]}, c.as_of_date


def test_six_curated_cards() -> None:
    assert len(CARD_IDS) == 6


@pytest.mark.parametrize("scenario_id", CARD_IDS)
def test_every_curated_card_parses_and_matches_the_scenario(scenario_id: str) -> None:
    folder = SCENARIOS_ROOT / scenario_id
    acct = rep_account_from_card((folder / "rep_card.md").read_text(encoding="utf-8"))
    offer = json.loads((folder / "offer.json").read_text())
    sim = json.loads((folder / "sim.json").read_text())
    assert acct["creditor"] == {
        "name": offer["creditor"],
        "outstanding_balance_cents": offer["creditor_balance_cents"],
        "original_balance_cents": offer["original_balance_cents"],
    }
    rules = acct["rules"]
    assert rules["max_payments"] == sim["rules"]["max_payments"]
    assert rules["min_payment_cents"] == sim["rules"]["min_payment_cents"]
    assert rules["structure"] == sim["rules"]["payment_structure"]
    assert rules["opening_ask_bp"] == sim["opening_ask_bp"]
    # Not every card states a floor; when it does, it is the sim's floor.
    assert rules["floor_bp"] in (None, sim["floor_bp"])


def test_easy_deal_card_values() -> None:
    acct = rep_account_from_card((SCENARIOS_ROOT / "easy_deal" / "rep_card.md").read_text())
    assert acct["rules"] == {
        "max_payments": 8,
        "min_payment_cents": 10000,
        "structure": "even",
        "opening_ask_bp": 4500,
        "floor_bp": 4000,
        "first_payment": None,
    }


def test_template_card_parses() -> None:
    acct = rep_account_from_card(SCENARIO_TEMPLATE["rep_card"])
    assert acct["creditor"]["outstanding_balance_cents"] == 125000
    assert acct["rules"]["floor_bp"] == 4000


def test_malformed_and_unknown_rows_are_not_guessed() -> None:
    md = (
        "## Creditor account\n\n| Field | Value |\n|---|---|\n"
        "| Creditor | Acme |\n| Outstanding balance | about twelve hundred |\n"
        "| Original balance | $1,600.5 |\n| Client savings | $440.00 |\n\n"
        "## Your settlement rules\n\n| Rule | Value |\n|---|---|\n"
        "| Max payments | eight |\n| Minimum payment | $1,00 |\n| Structure | weekly |\n"
        "| Opening ask | 42.255% |\n| Floor | 40.5% |\n| Secret ceiling | 55% |\n"
        "| Broken | row | extra |\n"
    )
    acct = rep_account_from_card(md)
    assert acct["creditor"] == {
        "name": "Acme",
        "outstanding_balance_cents": None,
        "original_balance_cents": None,
    }
    assert acct["rules"] == {
        "max_payments": None,
        "min_payment_cents": None,
        "structure": None,
        "opening_ask_bp": None,
        "floor_bp": 4050,
        "first_payment": None,
    }


def test_tables_outside_their_heading_are_ignored() -> None:
    md = "## Suggested replies\n\n| Max payments | 9 |\n| Creditor | Nope |\n"
    acct = rep_account_from_card(md)
    assert acct["creditor"]["name"] is None
    assert acct["rules"]["max_payments"] is None


@pytest.mark.parametrize("scenario_id", CARD_IDS)
def test_rep_endpoint_leaks_no_client_or_firm_value(tmp_path: Path, scenario_id: str) -> None:
    with make_client(tmp_path) as c:
        r = c.get(f"/scenarios/{scenario_id}/rep")
        brief = c.get(f"/scenarios/{scenario_id}").json()
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == scenario_id
    assert set(body) == {"id", "creditor", "rules"}
    for key in _keys(body):
        assert not any(bad in key for bad in _CLIENT_KEYS), key
    private, ref = _private(scenario_id)
    assert len(private) > 5
    assert leaked_private_values([body], private, ref=ref) == []
    # Sanity: the same scan does flag the operator brief.
    assert leaked_private_values([brief], private, ref=ref) != []


@pytest.mark.parametrize("scenario_id", CARD_IDS)
def test_rep_endpoint_has_no_ledger_amount_or_running_balance(
    tmp_path: Path, scenario_id: str
) -> None:
    """[P35] The negotiator's ledger table (entries and running balances) stays off ``/rep``."""
    with make_client(tmp_path) as c:
        body = c.get(f"/scenarios/{scenario_id}/rep").json()
        client = c.get(f"/scenarios/{scenario_id}").json()["client"]
    balance = client["sda_balance_cents"]
    running: set[tuple[str, int | date]] = set()
    for e in client["ledger"]:
        if e["scheduled"]:
            balance += e["amount_cents"] if e["type"] == "credit" else -e["amount_cents"]
            running.add(("money", balance))
    amounts = {("money", e["amount_cents"]) for e in client["ledger"]}
    assert amounts
    scan = {p for p in amounts | running if p[1]}
    _, ref = _private(scenario_id)
    assert leaked_private_values([body], scan, ref=ref) == []
    assert leaked_private_values([client], scan, ref=ref) != []


def test_rep_endpoint_unknown_id_is_404(tmp_path: Path) -> None:
    with make_client(tmp_path) as c:
        assert c.get("/scenarios/nope/rep").status_code == 404
        assert c.get("/scenarios/..%2Ffixtures/rep").status_code == 404


def test_balloon_structure_card_shows_the_sim_floor() -> None:
    """[34.1] The human rep sees the same floor the simulated rep holds."""
    folder = SCENARIOS_ROOT / "balloon_structure"
    acct = rep_account_from_card((folder / "rep_card.md").read_text(encoding="utf-8"))
    sim = json.loads((folder / "sim.json").read_text())
    assert acct["rules"]["floor_bp"] == sim["floor_bp"] == 3500
