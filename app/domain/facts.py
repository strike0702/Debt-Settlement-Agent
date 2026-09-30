"""Typed facts with PUBLIC/PRIVATE visibility for NLG and guards."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.units import render_count, render_date, render_money, render_pct

FactKind = Literal["money", "pct", "count", "date"]
Visibility = Literal["PUBLIC", "PRIVATE"]
FactSource = Literal["engine", "creditor", "config"]


class Fact(BaseModel):
    id: str
    kind: FactKind
    value: int | date
    visibility: Visibility
    source: FactSource

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
        if self.kind == "date":
            assert isinstance(self.value, date)
            return render_date(self.value, ref)
        raise ValueError(f"unknown fact kind: {self.kind}")


class FactSet(BaseModel):
    facts: dict[str, Fact] = Field(default_factory=dict)

    def add(self, fact: Fact) -> None:
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


# Visibility guide (section 4.3) — used by later phases when tagging engine output.
PRIVATE_FACT_IDS: frozenset[str] = frozenset(
    {
        "draft_amount",
        "sda_balance",
        "program_fee",
        "max_affordable_bp",
    }
)
