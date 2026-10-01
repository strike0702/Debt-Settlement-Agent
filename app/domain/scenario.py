"""Load a call scenario from a fixture folder (client + offer + firm).

A scenario is the synthetic case under negotiation: engine ``Client``, public
creditor offer amounts, and firm fee settings. Loaders read ``client.json``,
``offer.json``, and ``firm.json`` from a directory such as ``fixtures/demo``.
Optional ``rebase_to`` shifts every client date by whole months so demos do
not go stale relative to ``date.today()``. ``scenario_details`` builds the
operator-only brief (includes PRIVATE client finances; never sent to NLG).
"""

from __future__ import annotations

import json
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


def load_scenario(
    path: str | Path,
    *,
    rebase_to: date | None = None,
) -> CallScenario:
    """Load `client.json`, `offer.json`, and `firm.json` from a folder."""
    folder = Path(path)
    client = load_client(folder / "client.json")
    if rebase_to is not None:
        client = rebase_client(client, rebase_to)

    offer_raw = json.loads((folder / "offer.json").read_text())
    firm_raw = json.loads((folder / "firm.json").read_text())

    return CallScenario(
        id=folder.name,
        client=client,
        creditor=str(offer_raw["creditor"]),
        creditor_balance_cents=int(offer_raw["creditor_balance_cents"]),
        original_balance_cents=int(offer_raw["original_balance_cents"]),
        program_fee_pct=float(firm_raw["program_fee_pct"]),
        bank_fee_cents=int(firm_raw["bank_fee_cents"]),
    )


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
    c = sc.client
    upcoming = [e for e in c.ledger if e.date > c.as_of_date]
    deposits = sum(e.amount_cents for e in upcoming if e.type == "credit")
    withdrawals = sum(e.amount_cents for e in upcoming if e.type == "debit")
    fee_bp = int((Decimal(str(sc.program_fee_pct)) * 10000).to_integral_value(ROUND_HALF_UP))
    program_fee_cents = int(
        (Decimal(fee_bp) / Decimal(10000) * sc.original_balance_cents).to_integral_value(
            ROUND_HALF_UP
        )
    )
    return {
        "id": meta.id,
        "title": meta.title,
        "description": meta.description,
        "expected": meta.expected,
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
        },
        "firm": {
            "program_fee_bp": fee_bp,
            "program_fee_cents": program_fee_cents,
            "bank_fee_cents": sc.bank_fee_cents,
        },
    }
