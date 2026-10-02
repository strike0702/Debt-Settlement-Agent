"""Commitment-guard edge cases (REVIEW F23)."""

from __future__ import annotations

from datetime import date

from app.agent.guards import rendered_guard


def test_bare_deal_not_commitment() -> None:
    """F23: reviewing deal terms must not trip commitment guard."""
    rg = rendered_guard(
        "Let us review the deal terms carefully.",
        {},
        set(),
        set(),
        ref=date(2026, 1, 1),
    )
    assert rg.ok
