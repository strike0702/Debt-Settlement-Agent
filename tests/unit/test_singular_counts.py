"""A payment count of 1 is spoken "1 payment" ("1 payment of $500"), never "1 payments".

Covers every shipped bank line with a payment-count placeholder (rendered
through the real fill and guard path), the default templates, and the bank
build's acceptance check that keeps a line the singular rule cannot fix out
of the bank.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import date
from pathlib import Path
from types import ModuleType

from app.agent.nlg import (
    PAYMENT_COUNT_IDS,
    SAFE_FALLBACK,
    TEMPLATES,
    _render_filled,
    singular_count_template,
)
from app.agent.nlg_bank import DEFAULT_BANK_PATH
from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact

_REF = date(2026, 10, 1)
_PLURAL_AFTER_ONE = re.compile(r"\b1 (?:\w+ ){0,2}(?:payments|installments)\b")


def _builder() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_template_bank.py"
    spec = importlib.util.spec_from_file_location("build_template_bank", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fact(pid: str) -> Fact:
    if pid in PAYMENT_COUNT_IDS:
        kind, value = "count", 1
    elif "date" in pid:
        kind, value = "date", date(2026, 11, 3)
    elif "pct" in pid:
        kind, value = "pct", 4500
    else:
        kind, value = "money", 50000
    return Fact(id=pid, kind=kind, value=value, visibility="PUBLIC", source="engine")  # type: ignore[arg-type]


def _render(template: str) -> str:
    ids = set(re.findall(r"\{(\w+)\}", template))
    action = Action(
        intent=Intent.CONFIRM_SCHEDULE,
        facts={pid: _fact(pid) for pid in ids},
        required=ids,
        next_phase=Phase.CONFIRM,
    )
    return " ".join(
        _render_filled(
            template,
            action,
            _REF,
            creditor_numbers=None,
            private_blocklist=None,
            audit=None,
            call_id=None,
        )
    )


def _count_lines() -> list[tuple[str, str]]:
    bank = json.loads(DEFAULT_BANK_PATH.read_text(encoding="utf-8"))
    return [
        (e["intent"], t)
        for e in bank["entries"]
        if PAYMENT_COUNT_IDS.intersection(e["placeholders"])
        for t in e["templates"]
    ]


def test_no_rendered_bank_line_says_1_payments() -> None:
    lines = _count_lines()
    assert len(lines) >= 20
    for intent, template in lines:
        spoken = _render(template)
        assert spoken != SAFE_FALLBACK, template
        assert not _PLURAL_AFTER_ONE.search(spoken), (intent, spoken)
        if intent == "CONFIRM_SCHEDULE":
            assert re.search(r"\b1 (?:payment|installment) of \$500\b", spoken), spoken


def test_default_schedule_template_says_1_payment_of() -> None:
    spoken = _render(TEMPLATES[Intent.CONFIRM_SCHEDULE])
    assert "We can do 1 payment of $500" in spoken


def test_singular_rule_keeps_plural_counts_alone() -> None:
    line = "We can do {num_payments} payments totaling {offer_total}."
    assert singular_count_template(line, set()) == line
    assert singular_count_template(line, {"num_payments"}) == (
        "We can do {num_payments} payment of {offer_total}."
    )


def test_bank_build_rejects_lines_the_singular_rule_cannot_fix() -> None:
    mod = _builder()
    ids = ("first_payment_date", "num_payments", "offer_total", "settlement_pct")
    tail = " totaling {offer_total} starting {first_payment_date}."
    good = "{settlement_pct} works. We can do {num_payments} payments" + tail
    bad = "{settlement_pct} works. We can do {num_payments} monthly payments" + tail
    assert mod.template_ok(good, ids)
    assert not mod.template_ok(bad, ids)
    # Every shipped line would pass today's build.
    for _intent, template in _count_lines():
        ph = tuple(sorted(set(re.findall(r"\{(\w+)\}", template))))
        assert mod.singular_ok(template, ph), template
