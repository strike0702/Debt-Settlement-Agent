"""Sim creditor: schedule validation, 7-field reveal schedule, COUNTER_TERMS."""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact
from sim.creditor import LATE_FIELDS, CreditorPolicy
from sim.scenarios import generate_one


def _action(intent: Intent, facts: dict[str, Fact] | None = None, reason: str = "t") -> Action:
    return Action(
        intent=intent,
        text_slots={},
        facts=facts or {},
        required=set(),
        next_phase=Phase.NEGOTIATE,
        reason=reason,
    )


def _fact(fid: str, kind: str, value: int | date) -> Fact:
    return Fact(id=fid, kind=kind, value=value, visibility="PUBLIC", source="engine")  # type: ignore[arg-type]


def _creditor() -> CreditorPolicy:
    return CreditorPolicy(generate_one("flexible", "deal", seed=101), phrasing="template")


@pytest.mark.asyncio
async def test_speak_schedule_validates_like_confirm() -> None:
    """F15: SPEAK_SCHEDULE must not auto-accept without schedule checks."""
    creditor = _creditor()
    reply = await creditor.respond(_action(Intent.SPEAK_SCHEDULE), agent_text="here is a schedule")
    # Missing settlement facts → reject (same path as CONFIRM_SCHEDULE).
    assert reply.analysis.stance == "reject"


@pytest.mark.asyncio
async def test_opening_reveals_core_four_truthfully() -> None:
    creditor = _creditor()
    tr = creditor.scenario.true_rules
    reply = await creditor.respond(_action(Intent.OPENING))
    got = {t.field: t.value for t in reply.analysis.terms}
    assert got == {
        "max_payments": tr.max_payments,
        "min_payment_cents": tr.min_payment_cents,
        "payment_structure": tr.payment_structure,
        "first_payment_date": tr.first_payment_date,
    }
    for term in reply.analysis.terms:
        assert term.quote in reply.text


@pytest.mark.asyncio
async def test_late_fields_revealed_once_at_schedule_readback() -> None:
    creditor = _creditor()
    tr = creditor.scenario.true_rules
    await creditor.respond(_action(Intent.OPENING))
    first = await creditor.respond(_action(Intent.CONFIRM_SCHEDULE))
    got = {t.field: t.value for t in first.analysis.terms}
    assert got["max_segments"] == tr.max_segments
    assert got["max_token_pays"] == tr.max_token_pays
    assert got["min_payment_tiers"] == list(tr.min_payment_tiers)
    second = await creditor.respond(_action(Intent.CONFIRM_SCHEDULE))
    assert not {t.field for t in second.analysis.terms} & set(LATE_FIELDS)


@pytest.mark.asyncio
async def test_ask_reveals_late_field_with_matching_quote() -> None:
    creditor = _creditor()
    reply = await creditor.respond(_action(Intent.ASK, reason="max_segments"))
    (term,) = reply.analysis.terms
    assert term.field == "max_segments"
    assert term.value == creditor.scenario.true_rules.max_segments
    assert term.quote in reply.text


@pytest.mark.asyncio
async def test_counter_terms_rejects_outside_hidden_rules() -> None:
    creditor = _creditor()
    tr = creditor.scenario.true_rules
    lower_min = _fact("alt_min_payment_cents", "money", tr.min_payment_cents - 1000)
    reply = await creditor.respond(_action(Intent.COUNTER_TERMS, {lower_min.id: lower_min}))
    assert reply.analysis.stance == "reject"
    assert reply.analysis.terms == []
    assert creditor.agreed_rules == tr


@pytest.mark.asyncio
async def test_counter_terms_accepts_inside_hidden_rules_and_records_agreement() -> None:
    creditor = _creditor()
    tr = creditor.scenario.true_rules
    fewer = _fact("alt_max_payments", "count", tr.max_payments - 1)
    reply = await creditor.respond(_action(Intent.COUNTER_TERMS, {fewer.id: fewer}))
    assert reply.analysis.stance == "accept"
    assert creditor.agreed_rules.max_payments == tr.max_payments - 1
    assert creditor.rules.max_payments == tr.max_payments - 1


@pytest.mark.asyncio
async def test_counter_terms_later_start_rejected_earlier_accepted() -> None:
    creditor = _creditor()
    fpd = creditor.scenario.true_rules.first_payment_date
    later = _fact("alt_first_payment_date", "date", date(fpd.year, fpd.month + 1, 28))
    reply = await creditor.respond(_action(Intent.COUNTER_TERMS, {later.id: later}))
    assert reply.analysis.stance == "reject"
    earlier = _fact("alt_first_payment_date", "date", fpd.replace(day=fpd.day - 1))
    reply = await creditor.respond(_action(Intent.COUNTER_TERMS, {earlier.id: earlier}))
    assert reply.analysis.stance == "accept"
    assert creditor.agreed_rules.first_payment_date == earlier.value
