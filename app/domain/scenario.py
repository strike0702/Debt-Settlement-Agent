"""Call scenario loader: client, public offer fields, and firm fees."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from feasibility.models import Client, load_client


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


def load_scenario(path: str | Path) -> CallScenario:
    """Load `client.json`, `offer.json`, and `firm.json` from a folder."""
    folder = Path(path)
    client = load_client(folder / "client.json")

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
