"""Load a call scenario from a fixture folder (client + offer + firm).

A scenario is the synthetic case under negotiation: engine ``Client``, public
creditor offer amounts, and firm fee settings. Loaders read ``client.json``,
``offer.json``, and ``firm.json`` from a directory such as ``fixtures/demo``.
Optional ``rebase_to`` shifts every client date by whole months so demos do
not go stale relative to ``date.today()``. ``scenario_details`` builds the
operator-only brief (includes PRIVATE client finances; never sent to NLG).
``rep_account`` is the opposite: the creditor's own account and rules, parsed
from ``rep_card.md`` only, for the human playing the rep (no client data).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from feasibility.models import Client, LedgerEntry, add_months, load_client

SCENARIOS_ROOT = Path("fixtures/scenarios")


@dataclass(frozen=True)
class CallScenario:
    """One outbound call's inputs.

    Offer keeps only public fields the agent may speak. Settlement ask and
    creditor rules come from the negotiation / belief state, not this file.
    Firm fees are known to us and never asked of the rep.
    """

    id: str
    client: Client
    creditor: str
    creditor_balance_cents: int
    original_balance_cents: int
    program_fee_pct: float
    bank_fee_cents: int


@dataclass(frozen=True)
class ScenarioMeta:
    """Catalog entry for the operator scenario picker."""

    id: str
    title: str
    description: str
    expected: str  # deal | counter | escalate | no_deal


def _months_delta(from_d: date, to_d: date) -> int:
    """Whole-month shift so ``from_d``'s month lands on ``to_d``'s month."""
    return (to_d.year - from_d.year) * 12 + (to_d.month - from_d.month)


def rebase_client(client: Client, to: date) -> Client:
    """Shift all client dates by whole months; keep ``draft_day``."""
    delta = _months_delta(client.as_of_date, to)
    if delta == 0:
        return client
    return Client(
        draft_amount_cents=client.draft_amount_cents,
        draft_day=client.draft_day,
        first_draft_date=add_months(client.first_draft_date, delta),
        last_draft_date=add_months(client.last_draft_date, delta),
        as_of_date=add_months(client.as_of_date, delta),
        current_balance_cents=client.current_balance_cents,
        ledger=[
            LedgerEntry(
                date=add_months(e.date, delta),
                amount_cents=e.amount_cents,
                type=e.type,
            )
            for e in client.ledger
        ],
    )


def client_from_raw(raw: dict[str, Any]) -> Client:
    """Build an engine ``Client`` from a JSON-shaped dict (file or upload)."""
    return Client(
        draft_amount_cents=int(raw["draft_amount_cents"]),
        draft_day=int(raw["draft_day"]),
        first_draft_date=date.fromisoformat(str(raw["first_draft_date"])),
        last_draft_date=date.fromisoformat(str(raw["last_draft_date"])),
        as_of_date=date.fromisoformat(str(raw["as_of_date"])),
        current_balance_cents=int(raw["current_balance_cents"]),
        ledger=[
            LedgerEntry(
                date=date.fromisoformat(str(e["date"])),
                amount_cents=int(e["amount_cents"]),
                type=e["type"],
            )
            for e in raw.get("ledger", [])
        ],
    )


def scenario_from_parts(
    *,
    scenario_id: str,
    client: Client,
    offer_raw: dict[str, Any],
    firm_raw: dict[str, Any],
    rebase_to: date | None = None,
) -> CallScenario:
    """Assemble a ``CallScenario`` from offer/firm dicts and a ``Client``."""
    if rebase_to is not None:
        client = rebase_client(client, rebase_to)
    return CallScenario(
        id=scenario_id,
        client=client,
        creditor=str(offer_raw["creditor"]),
        creditor_balance_cents=int(offer_raw["creditor_balance_cents"]),
        original_balance_cents=int(offer_raw["original_balance_cents"]),
        program_fee_pct=float(firm_raw["program_fee_pct"]),
        bank_fee_cents=int(firm_raw["bank_fee_cents"]),
    )


def scenario_from_payload(
    payload: dict[str, Any],
    *,
    scenario_id: str = "custom",
    rebase_to: date | None = None,
) -> CallScenario:
    """Build a scenario from an uploaded/pasted test-case JSON object.

    Expected keys: ``client``, ``offer``, ``firm``. Optional ``meta.id`` overrides
    ``scenario_id``. Raises ``ValueError`` on missing/invalid fields.
    """
    try:
        client_raw = payload["client"]
        offer_raw = payload["offer"]
        firm_raw = payload["firm"]
    except KeyError as e:
        raise ValueError(f"scenario payload missing {e.args[0]}") from e
    meta = payload.get("meta") or {}
    sid = str(meta.get("id") or scenario_id).strip() or scenario_id
    if "/" in sid or "\\" in sid or sid.startswith(".") or ".." in sid:
        raise ValueError(f"invalid scenario id: {sid!r}")
    try:
        client = client_from_raw(client_raw)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"invalid client: {e}") from e
    try:
        return scenario_from_parts(
            scenario_id=sid,
            client=client,
            offer_raw=offer_raw,
            firm_raw=firm_raw,
            rebase_to=rebase_to,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"invalid offer/firm: {e}") from e


def load_scenario(
    path: str | Path,
    *,
    rebase_to: date | None = None,
) -> CallScenario:
    """Load `client.json`, `offer.json`, and `firm.json` from a folder."""
    folder = Path(path)
    client = load_client(folder / "client.json")
    offer_raw = json.loads((folder / "offer.json").read_text())
    firm_raw = json.loads((folder / "firm.json").read_text())
    return scenario_from_parts(
        scenario_id=folder.name,
        client=client,
        offer_raw=offer_raw,
        firm_raw=firm_raw,
        rebase_to=rebase_to,
    )


SCENARIO_TEMPLATE: dict[str, Any] = {
    "meta": {
        "id": "custom",
        "title": "My test case",
        "description": "Paste or edit this template, then Apply.",
        "expected": "deal",
    },
    "offer": {
        "creditor": "NorthPeak Collections",
        "creditor_balance_cents": 125000,
        "original_balance_cents": 160000,
    },
    "firm": {
        "program_fee_pct": 0.18,
        "bank_fee_cents": 950,
    },
    "client": {
        "draft_amount_cents": 22000,
        "draft_day": 15,
        "first_draft_date": "2026-03-15",
        "last_draft_date": "2026-10-15",
        "as_of_date": "2026-03-01",
        "current_balance_cents": 44000,
        "ledger": [
            {"date": "2026-03-15", "amount_cents": 22000, "type": "credit"},
            {"date": "2026-04-15", "amount_cents": 22000, "type": "credit"},
            {"date": "2026-05-15", "amount_cents": 22000, "type": "credit"},
            {"date": "2026-06-15", "amount_cents": 22000, "type": "credit"},
            {"date": "2026-07-15", "amount_cents": 22000, "type": "credit"},
            {"date": "2026-08-15", "amount_cents": 22000, "type": "credit"},
            {"date": "2026-09-15", "amount_cents": 22000, "type": "credit"},
            {"date": "2026-10-15", "amount_cents": 22000, "type": "credit"},
        ],
    },
    "rep_card": (
        "# Creditor script\n\n"
        "You are the **creditor collections rep**. The agent negotiates for the client.\n"
        "Do not ask for the client's income, SDA balance, or draft amount.\n\n"
        "## Creditor account\n\n"
        "| Field | Value |\n|---|---|\n"
        "| Creditor | NorthPeak Collections |\n"
        "| Outstanding balance | $1,250.00 |\n"
        "| Original balance | $1,600.00 |\n\n"
        "## Your settlement rules\n\n"
        "| Rule | Value |\n|---|---|\n"
        "| Max payments | 8 |\n"
        "| Minimum payment | $100 |\n"
        "| Structure | even |\n"
        "| Opening ask | 45% of balance |\n"
        "| Floor | 40% (do not go below) |\n"
    ),
}


def resolve_scenario_dir(scenario_id: str, *, root: Path | None = None) -> Path:
    """Resolve a catalog id under ``fixtures/scenarios`` (no path traversal)."""
    base = (root or SCENARIOS_ROOT).resolve()
    sid = scenario_id.strip().replace("\\", "/")
    if not sid or "/" in sid or sid.startswith(".") or ".." in sid:
        raise ValueError(f"invalid scenario id: {scenario_id!r}")
    folder = (base / sid).resolve()
    if not folder.is_dir() or folder.parent != base:
        raise FileNotFoundError(f"scenario not found: {scenario_id}")
    return folder


def list_scenario_metas(*, root: Path | None = None) -> list[ScenarioMeta]:
    """Read ``meta.json`` from each scenario folder (sorted by id)."""
    base = root or SCENARIOS_ROOT
    if not base.is_dir():
        return []
    out: list[ScenarioMeta] = []
    for folder in sorted(base.iterdir()):
        if not folder.is_dir():
            continue
        meta_path = folder / "meta.json"
        if not meta_path.is_file():
            continue
        raw = json.loads(meta_path.read_text())
        out.append(
            ScenarioMeta(
                id=folder.name,
                title=str(raw.get("title") or folder.name),
                description=str(raw.get("description") or ""),
                expected=str(raw.get("expected") or "deal"),
            )
        )
    return out


def load_rep_card(scenario_id: str, *, root: Path | None = None) -> str:
    """Return the markdown rep card for a catalog scenario."""
    folder = resolve_scenario_dir(scenario_id, root=root)
    path = folder / "rep_card.md"
    if not path.is_file():
        return ""
    return path.read_text()


def rep_card_suggestions(markdown: str) -> list[str]:
    """Bullet lines under the rep card's ``## Suggested replies`` heading, in order."""
    out: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == "suggested replies"
            continue
        if inside and line.startswith("- "):
            out.append(line[2:].strip())
    return out


# Rep card table rows → typed values. A label not listed here is skipped (never guessed);
# a listed label whose value does not parse comes back as ``None``.
_MONEY_RE = re.compile(r"^\$(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?(?![\d,.])")
_PCT_RE = re.compile(r"^(\d+(?:\.\d+)?)%")
_COUNT_RE = re.compile(r"^(\d+)(?![\d.,%])")
_STRUCTURES = ("even", "balloon", "flexible")
_RowParser = Callable[[str], int | str | None]


def _money_cents(text: str) -> int | None:
    m = _MONEY_RE.match(text)
    if not m:
        return None
    return int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)


def _pct_bp(text: str) -> int | None:
    m = _PCT_RE.match(text)
    if not m:
        return None
    bp = Decimal(m.group(1)) * 100
    return int(bp) if bp == bp.to_integral_value() else None


def _count(text: str) -> int | None:
    m = _COUNT_RE.match(text)
    return int(m.group(1)) if m else None


def _structure(text: str) -> str | None:
    m = re.match(r"[a-z]+", text.lower())
    return m.group(0) if m and m.group(0) in _STRUCTURES else None


def _free_text(text: str) -> str | None:
    return text or None


_ACCOUNT_ROWS: dict[str, tuple[str, _RowParser]] = {
    "creditor": ("name", _free_text),
    "outstanding balance": ("outstanding_balance_cents", _money_cents),
    "original balance": ("original_balance_cents", _money_cents),
}
_RULE_ROWS: dict[str, tuple[str, _RowParser]] = {
    "max payments": ("max_payments", _count),
    "minimum payment": ("min_payment_cents", _money_cents),
    "structure": ("structure", _structure),
    "opening ask": ("opening_ask_bp", _pct_bp),
    "floor": ("floor_bp", _pct_bp),
    "first payment": ("first_payment", _free_text),
}


def _table_rows(markdown: str, heading: str) -> list[tuple[str, str]]:
    """``(label, value)`` cells of the first two-column table under ``## heading``."""
    out: list[tuple[str, str]] = []
    inside = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2 or set(cells[1]) <= set("-: "):
            continue  # separator row or a malformed row
        out.append((cells[0].lower(), cells[1]))
    return out


def _parse_rows(
    markdown: str, heading: str, rows: dict[str, tuple[str, _RowParser]]
) -> dict[str, Any]:
    parsed: dict[str, Any] = {key: None for key, _ in rows.values()}
    for label, value in _table_rows(markdown, heading):
        if label in rows:
            key, parse = rows[label]
            parsed[key] = parse(value)
    return parsed


def rep_account_from_card(markdown: str) -> dict[str, Any]:
    """Rep-safe view of a rep card: the creditor's own account and settlement rules.

    Reads only the ``## Creditor account`` and ``## Your settlement rules`` tables.
    Money is integer cents, percentages basis points; a known row that does not
    parse is ``None``, an unknown row is dropped. Holds no client or firm data.
    """
    return {
        "creditor": _parse_rows(markdown, "creditor account", _ACCOUNT_ROWS),
        "rules": _parse_rows(markdown, "your settlement rules", _RULE_ROWS),
    }


def rep_account(scenario_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """``rep_account_from_card`` for a catalog scenario; raises like ``resolve_scenario_dir``."""
    folder = resolve_scenario_dir(scenario_id, root=root)
    return {"id": folder.name, **rep_account_from_card(load_rep_card(folder.name, root=root))}


def details_from_scenario(
    sc: CallScenario,
    *,
    title: str | None = None,
    description: str = "",
    expected: str = "deal",
) -> dict[str, Any]:
    """Operator brief dict for a loaded scenario (catalog or custom upload)."""
    c = sc.client
    upcoming = sorted((e for e in c.ledger if e.date > c.as_of_date), key=lambda e: e.date)
    deposits = sum(e.amount_cents for e in upcoming if e.type == "credit")
    withdrawals = sum(e.amount_cents for e in upcoming if e.type == "debit")
    fee_bp = int((Decimal(str(sc.program_fee_pct)) * 10000).to_integral_value(ROUND_HALF_UP))
    program_fee_cents = int(
        (Decimal(fee_bp) / Decimal(10000) * sc.original_balance_cents).to_integral_value(
            ROUND_HALF_UP
        )
    )
    return {
        "id": sc.id,
        "title": title or sc.id,
        "description": description,
        "expected": expected,
        "creditor": {
            "name": sc.creditor,
            "creditor_balance_cents": sc.creditor_balance_cents,
            "original_balance_cents": sc.original_balance_cents,
        },
        "client": {
            "as_of_date": c.as_of_date.isoformat(),
            "sda_balance_cents": c.current_balance_cents,
            "draft_amount_cents": c.draft_amount_cents,
            "draft_day": c.draft_day,
            "first_draft_date": c.first_draft_date.isoformat(),
            "last_draft_date": c.last_draft_date.isoformat(),
            "upcoming_drafts": sum(1 for e in upcoming if e.type == "credit"),
            "upcoming_deposits_cents": deposits,
            "upcoming_withdrawals_cents": withdrawals,
            # Engine ledger after as-of: credits = SDA deposits; debits = scheduled pulls.
            "upcoming_ledger": [
                {
                    "date": e.date.isoformat(),
                    "amount_cents": e.amount_cents,
                    "type": e.type,
                }
                for e in upcoming
            ],
        },
        "firm": {
            "program_fee_bp": fee_bp,
            "program_fee_cents": program_fee_cents,
            "bank_fee_cents": sc.bank_fee_cents,
        },
    }


def scenario_details(
    scenario_id: str,
    *,
    rebase_to: date | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Operator brief for one catalog scenario, including PRIVATE client finances.

    Dates are rebased like the live call (``rebase_to``) so the brief matches
    what the engine will see. Money is integer cents; fee rate is basis points.
    """
    folder = resolve_scenario_dir(scenario_id, root=root)
    meta = next(
        (m for m in list_scenario_metas(root=root) if m.id == folder.name),
        ScenarioMeta(id=folder.name, title=folder.name, description="", expected="deal"),
    )
    sc = load_scenario(folder, rebase_to=rebase_to)
    return details_from_scenario(
        sc,
        title=meta.title,
        description=meta.description,
        expected=meta.expected,
    )
