"""What we currently believe about each creditor term (and how sure we are).

Tracks per-field status (UNKNOWN → TENTATIVE/KNOWN/…), evidence quotes, and
history. Engine adapters only consume fields that are ``usable_for_engine``.
NLU observations and read-back confirmations flow through ``observe`` /
``confirm_readback``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.fields import FIELD_REGISTRY, FIELDS_BY_NAME
from feasibility.models import Client, default_first_payment_date


class TermStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    TENTATIVE = "TENTATIVE"
    KNOWN = "KNOWN"
    CONTRADICTED = "CONTRADICTED"
    ASSUMED = "ASSUMED"


class Evidence(BaseModel):
    turn: int
    quote: str


class TermBelief(BaseModel):
    field: str
    value: Any = None
    status: TermStatus = TermStatus.UNKNOWN
    evidence: list[Evidence] = Field(default_factory=list)
    history: list[Any] = Field(default_factory=list)


class BeliefChange(BaseModel):
    field: str
    old_value: Any = None
    new_value: Any = None
    old_status: TermStatus
    new_status: TermStatus
    turn: int
    quote: str | None = None


def _values_equal(a: Any, b: Any) -> bool:
    return a == b


class BeliefState:
    """Per-call beliefs about creditor rules."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.terms: dict[str, TermBelief] = {}
        for spec in FIELD_REGISTRY:
            if spec.name == "first_payment_date":
                self.terms[spec.name] = TermBelief(
                    field=spec.name,
                    value=default_first_payment_date(client),
                    status=TermStatus.ASSUMED,
                )
            elif spec.name == "max_token_pays":
                # Default is max_payments (no limit); value synced while ASSUMED.
                self.terms[spec.name] = TermBelief(
                    field=spec.name,
                    value=None,
                    status=TermStatus.ASSUMED,
                )
            elif spec.default_factory is not None:
                self.terms[spec.name] = TermBelief(
                    field=spec.name,
                    value=spec.default_factory(),
                    status=TermStatus.ASSUMED,
                )
            else:
                self.terms[spec.name] = TermBelief(
                    field=spec.name,
                    value=None,
                    status=TermStatus.UNKNOWN,
                )

    def get(self, field: str) -> TermBelief:
        return self.terms[field]

    def usable_for_engine(self, field: str) -> bool:
        return self.terms[field].status in (TermStatus.KNOWN, TermStatus.ASSUMED)

    def observe(
        self,
        field: str,
        value: Any,
        quote: str,
        turn: int,
        *,
        verified: bool,
        hedged: bool,
    ) -> BeliefChange:
        if field not in self.terms:
            raise KeyError(field)
        if field not in FIELDS_BY_NAME:
            raise KeyError(field)

        strong = verified and not hedged
        term = self.terms[field]
        old_value = term.value
        old_status = term.status
        evidence = Evidence(turn=turn, quote=quote)

        if old_status in (TermStatus.UNKNOWN, TermStatus.ASSUMED):
            term.value = value
            term.status = TermStatus.KNOWN if strong else TermStatus.TENTATIVE
            term.evidence.append(evidence)
            self._maybe_sync_max_token_pays(field, value)
            return BeliefChange(
                field=field,
                old_value=old_value,
                new_value=term.value,
                old_status=old_status,
                new_status=term.status,
                turn=turn,
                quote=quote,
            )

        if _values_equal(old_value, value):
            if old_status == TermStatus.TENTATIVE and strong:
                term.status = TermStatus.KNOWN
            term.evidence.append(evidence)
            self._maybe_sync_max_token_pays(field, value)
            return BeliefChange(
                field=field,
                old_value=old_value,
                new_value=term.value,
                old_status=old_status,
                new_status=term.status,
                turn=turn,
                quote=quote,
            )

        if old_status == TermStatus.TENTATIVE:
            # Self-correction: replace the value.
            term.value = value
            term.status = TermStatus.KNOWN if strong else TermStatus.TENTATIVE
            term.evidence.append(evidence)
            self._maybe_sync_max_token_pays(field, value)
            return BeliefChange(
                field=field,
                old_value=old_value,
                new_value=term.value,
                old_status=old_status,
                new_status=term.status,
                turn=turn,
                quote=quote,
            )

        if old_status == TermStatus.KNOWN:
            term.history.append(old_value)
            term.value = value
            term.status = TermStatus.CONTRADICTED
            term.evidence.append(evidence)
            return BeliefChange(
                field=field,
                old_value=old_value,
                new_value=term.value,
                old_status=old_status,
                new_status=term.status,
                turn=turn,
                quote=quote,
            )

        if old_status == TermStatus.CONTRADICTED:
            # Rep answering our clarifying question.
            term.value = value
            term.status = TermStatus.KNOWN if strong else TermStatus.TENTATIVE
            term.evidence.append(evidence)
            self._maybe_sync_max_token_pays(field, value)
            return BeliefChange(
                field=field,
                old_value=old_value,
                new_value=term.value,
                old_status=old_status,
                new_status=term.status,
                turn=turn,
                quote=quote,
            )

        raise RuntimeError(f"unhandled status {old_status} for field {field}")

    def accept_alternative(
        self,
        field: str,
        value: Any,
        quote: str,
        turn: int,
    ) -> BeliefChange:
        """Agent-mediated term change (e.g. earlier start date) → KNOWN.

        Unlike ``observe``, this does not mark a prior KNOWN value as
        CONTRADICTED — the rep accepted our proposed alternative.
        """
        if field not in self.terms or field not in FIELDS_BY_NAME:
            raise KeyError(field)
        term = self.terms[field]
        old_value = term.value
        old_status = term.status
        if old_value is not None and not _values_equal(old_value, value):
            term.history.append(old_value)
        term.value = value
        term.status = TermStatus.KNOWN
        term.evidence.append(Evidence(turn=turn, quote=quote))
        self._maybe_sync_max_token_pays(field, value)
        return BeliefChange(
            field=field,
            old_value=old_value,
            new_value=term.value,
            old_status=old_status,
            new_status=term.status,
            turn=turn,
            quote=quote,
        )

    def confirm_readback(self, field: str, yes: bool) -> BeliefChange:
        if field not in self.terms:
            raise KeyError(field)
        term = self.terms[field]
        if term.status != TermStatus.TENTATIVE:
            raise ValueError(
                f"confirm_readback requires TENTATIVE, got {term.status} for {field}"
            )
        old_value = term.value
        old_status = term.status
        if yes:
            term.status = TermStatus.KNOWN
        else:
            term.value = None
            term.status = TermStatus.UNKNOWN
        return BeliefChange(
            field=field,
            old_value=old_value,
            new_value=term.value,
            old_status=old_status,
            new_status=term.status,
            turn=-1,
            quote=None,
        )

    def _maybe_sync_max_token_pays(self, field: str, value: Any) -> None:
        if field != "max_payments":
            return
        token = self.terms["max_token_pays"]
        if token.status == TermStatus.ASSUMED:
            token.value = value

    def missing_required(self) -> list[str]:
        """Required fields not yet usable for the engine, in registry order."""
        out: list[str] = []
        for spec in FIELD_REGISTRY:
            if spec.required and not self.usable_for_engine(spec.name):
                out.append(spec.name)
        return out

    def tentative_fields(self) -> list[str]:
        return [name for name, t in self.terms.items() if t.status == TermStatus.TENTATIVE]

    def contradicted_fields(self) -> list[str]:
        return [
            name for name, t in self.terms.items() if t.status == TermStatus.CONTRADICTED
        ]
