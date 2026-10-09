"""Typed negotiation figures with PUBLIC/PRIVATE visibility.

``Fact`` is the only source of spoken numbers (via ``render`` → ``units``).
The ``text`` kind (Phase 50) carries a fixed, digit-free phrase such as the
acknowledged payment structure ("a balloon schedule"); it is never a figure.
PUBLIC facts may go to NLG and the creditor-facing transcript; PRIVATE facts
(balances, fees, max affordable) stay off the NLG prompt and feed the
rendered-guard blocklist.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.domain.units import (
    render_count,
    render_date,
    render_money,
    render_ordinal,
    render_pct,
)

FactKind = Literal["money", "pct", "count", "date", "ordinal", "text"]
Visibility = Literal["PUBLIC", "PRIVATE"]
FactSource = Literal["engine", "creditor", "config"]


class Fact(BaseModel):
    id: str
    kind: FactKind
    value: int | date | str
    visibility: Visibility
    source: FactSource

    @model_validator(mode="after")
    def _text_only_for_text_kind(self) -> Fact:
        """A ``text`` fact is a digit-free string; every other kind is a number or date."""
        if self.kind == "text":
            if not isinstance(self.value, str) or any(ch.isdigit() for ch in self.value):
                raise ValueError("text fact must be a digit-free string")
        elif isinstance(self.value, str):
            raise ValueError(f"{self.kind} fact cannot hold a string")
        return self

    def render(self, ref: date) -> str:
        if self.kind == "money":
            assert isinstance(self.value, int)
            return render_money(self.value)
        if self.kind == "pct":
            assert isinstance(self.value, int)
            return render_pct(self.value)
        if self.kind == "count":
            assert isinstance(self.value, int)
            return render_count(self.value)
        if self.kind == "ordinal":
            assert isinstance(self.value, int)
            return render_ordinal(self.value)
        if self.kind == "date":
            assert isinstance(self.value, date)
            return render_date(self.value, ref)
        if self.kind == "text":
            assert isinstance(self.value, str)
            return self.value
        raise ValueError(f"unknown fact kind: {self.kind}")


# Exact private fact ids from the engine adapter (plus row prefixes below).
PRIVATE_FACT_IDS: frozenset[str] = frozenset(
    {
        "program_fee",
        "rescue_lump",
        "rescue_increment",
    }
)
_PRIVATE_ID_PREFIXES: tuple[str, ...] = ("balance_", "program_fee_", "bank_fee_")


def fact_id_must_be_private(fact_id: str) -> bool:
    """True when ``fact_id`` is in the private registry or a private row prefix."""
    if fact_id in PRIVATE_FACT_IDS:
        return True
    return any(fact_id.startswith(p) for p in _PRIVATE_ID_PREFIXES)


class FactSet(BaseModel):
    facts: dict[str, Fact] = Field(default_factory=dict)

    def add(self, fact: Fact) -> None:
        if fact_id_must_be_private(fact.id) and fact.visibility != "PRIVATE":
            raise ValueError(f"fact {fact.id!r} must be PRIVATE")
        self.facts[fact.id] = fact

    def get(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def __contains__(self, fact_id: object) -> bool:
        return isinstance(fact_id, str) and fact_id in self.facts

    def __getitem__(self, fact_id: str) -> Fact:
        return self.facts[fact_id]

    def public(self) -> dict[str, Fact]:
        return {k: v for k, v in self.facts.items() if v.visibility == "PUBLIC"}

    def private(self) -> dict[str, Fact]:
        return {k: v for k, v in self.facts.items() if v.visibility == "PRIVATE"}

    def ids(self) -> set[str]:
        return set(self.facts)

