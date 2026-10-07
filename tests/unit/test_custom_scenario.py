"""Phase 36: custom test cases from the UI (validation with field paths, rep-safe preview).

``POST /scenarios/preview`` answers a bad case with every problem and its
field path (``client.draft_day``) so the editor can show them inline.
``POST /scenarios/preview/rep`` is the Creditor rep view's "Your account" for
a pasted case: the creditor's own account and rules, never a client or firm
field (carry item 34.2).
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.domain.scenario import SCENARIO_TEMPLATE, scenario_payload_errors
from tests.wsutil import leaked_private_values, make_client

_CLIENT_KEYS = ("client", "sda", "draft", "deposit", "withdraw", "fee", "ledger", "as_of")


def _tpl() -> dict[str, Any]:
    return copy.deepcopy(SCENARIO_TEMPLATE)


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [k for k, v in value.items() for k in (k, *_keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in _keys(v)]
    return []


def test_template_has_no_errors() -> None:
    assert scenario_payload_errors(_tpl()) == []


@pytest.mark.parametrize(
    ("edit", "path"),
    [
        (lambda p: p.pop("client"), "client"),
        (lambda p: p["client"].pop("draft_day"), "client.draft_day"),
        (lambda p: p["client"].__setitem__("draft_day", 40), "client.draft_day"),
        (
            lambda p: p["client"].__setitem__("draft_amount_cents", 220.5),
            "client.draft_amount_cents",
        ),
        (lambda p: p["client"].__setitem__("as_of_date", "March 1"), "client.as_of_date"),
        (
            lambda p: p["client"]["ledger"][2].__setitem__("type", "deposit"),
            "client.ledger[2].type",
        ),
        (
            lambda p: p["client"]["ledger"][0].__setitem__("amount_cents", "220"),
            "client.ledger[0].amount_cents",
        ),
        (lambda p: p["offer"].__setitem__("creditor", ""), "offer.creditor"),
        (
            lambda p: p["offer"].__setitem__("creditor_balance_cents", True),
            "offer.creditor_balance_cents",
        ),
        (lambda p: p["firm"].__setitem__("program_fee_pct", 18), "firm.program_fee_pct"),
        (lambda p: p["meta"].__setitem__("id", "../x"), "meta.id"),
        (lambda p: p["meta"].__setitem__("expected", "win"), "meta.expected"),
        (lambda p: p.__setitem__("rep_card", 5), "rep_card"),
        (
            lambda p: p["client"].__setitem__("last_draft_date", "2026-01-15"),
            "client.last_draft_date",
        ),
    ],
)
def test_errors_name_the_field_path(edit: Any, path: str) -> None:
    payload = _tpl()
    edit(payload)
    errors = scenario_payload_errors(payload)
    assert path in [e["path"] for e in errors], errors
    assert all(e["message"] and e["message"][0].isupper() for e in errors)


def test_not_an_object() -> None:
    assert scenario_payload_errors([1, 2]) == [  # type: ignore[arg-type]
        {"path": "", "message": "The test case must be a JSON object."}
    ]


def test_preview_returns_every_error_with_its_path(tmp_path: Path) -> None:
    payload = _tpl()
    payload["client"]["draft_day"] = 0
    payload["offer"]["creditor"] = ""
    with make_client(tmp_path) as client:
        res = client.post("/scenarios/preview", json=payload)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert {e["path"] for e in detail["errors"]} == {"client.draft_day", "offer.creditor"}
    assert detail["message"]


def test_preview_rep_is_the_rep_account_of_the_pasted_case(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        res = client.post("/scenarios/preview/rep", json=_tpl())
    assert res.status_code == 200
    body = res.json()
    assert body["creditor"] == {
        "name": "NorthPeak Collections",
        "outstanding_balance_cents": 125_000,
        "original_balance_cents": 160_000,
    }
    assert body["rules"]["max_payments"] == 8
    assert body["rules"]["floor_bp"] == 4000
    assert body["suggested"] and body["suggested"][0].startswith("Sure.")


def test_preview_rep_falls_back_to_the_offer_without_a_card(tmp_path: Path) -> None:
    payload = _tpl()
    del payload["rep_card"]
    payload["offer"]["creditor"] = "Harbor Card Services"
    with make_client(tmp_path) as client:
        body = client.post("/scenarios/preview/rep", json=payload).json()
    assert body["creditor"]["name"] == "Harbor Card Services"
    assert body["creditor"]["outstanding_balance_cents"] == 125_000
    assert set(body["rules"].values()) == {None}
    assert body["suggested"] == []


def test_preview_rep_leaks_no_client_or_firm_field(tmp_path: Path) -> None:
    """[34.2] Distinctive client and firm values never reach the rep-safe preview."""
    payload = _tpl()
    c = payload["client"]
    c["current_balance_cents"] = 73_311
    c["draft_amount_cents"] = 21_977
    c["ledger"] = [{**e, "amount_cents": 21_977} for e in c["ledger"]]
    c["ledger"].append({"date": "2026-09-20", "amount_cents": 4_133, "type": "debit"})
    payload["firm"] = {"program_fee_pct": 0.1733, "bank_fee_cents": 977}
    with make_client(tmp_path) as client:
        body = client.post("/scenarios/preview/rep", json=payload).json()
        brief = client.post("/scenarios/preview", json=payload).json()
    for key in _keys(body):
        assert not any(bad in key for bad in _CLIENT_KEYS), key
    text = json.dumps(body)
    for raw in ("73311", "21977", "4133", "1733", "977", "2026-09-20"):
        assert raw not in text
    private: set[tuple[str, int | date]] = {
        ("money", 73_311),
        ("money", 21_977),
        ("money", 4_133),
        ("money", 977),
        ("pct", 1733),
    }
    assert leaked_private_values([body], private, ref=date.today()) == []
    # Sanity: the same scan flags the operator brief of the same case.
    assert leaked_private_values([brief], private, ref=date.today()) != []


def test_preview_rep_rejects_a_non_object(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.post("/scenarios/preview/rep", json=[1]).status_code == 422
