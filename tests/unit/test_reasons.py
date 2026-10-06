"""REASON_TEXT covers every reason code ``decide()`` and the orchestrator emit."""

from __future__ import annotations

import re
import string
from datetime import date
from pathlib import Path

import pytest

from app.agent.reasons import (
    PUBLIC_PLACEHOLDERS,
    REASON_TEXT,
    reason_key,
    reason_text,
)
from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact
from app.domain.fields import FIELDS_BY_NAME
from app.store.audit import AuditLog
from eval.run_eval import _build_settings, run_one_scenario
from sim.scenarios import generate

_REF = date(2026, 3, 1)
_LITERAL_REASON = re.compile(r'reason="([a-z_]+)"')


def _literal_codes(path: str) -> set[str]:
    return set(_LITERAL_REASON.findall(Path(path).read_text()))


def test_every_literal_reason_code_in_policy_and_orchestrator_has_text() -> None:
    codes = _literal_codes("app/agent/policy.py") | _literal_codes("app/agent/orchestrator.py")
    # Confirm-path reasons are passed positionally through ``confirm("...")``.
    codes |= set(re.findall(r'confirm\("([a-z_]+)"\)', Path("app/agent/policy.py").read_text()))
    assert codes, "regex found no reason codes"
    missing = sorted(c for c in codes if c not in REASON_TEXT)
    assert missing == []


def test_dynamic_reason_codes_normalize_to_keys() -> None:
    assert reason_key(Intent.COUNTER, "bp=4900") == "counter"
    assert reason_key(Intent.CONFIRM_SCHEDULE, "bp=4500") == "confirm"
    assert reason_key(Intent.CONFIRM_SCHEDULE, None) == "confirm"
    assert reason_key(Intent.ASK, "max_payments") == "ask_field"
    assert reason_key(Intent.READ_BACK, "min_payment_cents") == "read_back"
    assert reason_key(Intent.CLARIFY, "max_payments") == "clarify_field"
    assert reason_key(Intent.OPENING, None) == "opening"
    assert reason_key(Intent.ASK_SETTLEMENT, None) == "ask_settlement"
    assert reason_key(Intent.NO_DEAL_WRAP, "infeasible") == "infeasible"
    for field in FIELDS_BY_NAME:
        assert reason_key(Intent.ASK, field) in REASON_TEXT


def test_placeholders_are_public_only() -> None:
    fmt = string.Formatter()
    for key, text in REASON_TEXT.items():
        names = {n for _, n, _, _ in fmt.parse(text) if n}
        assert names <= PUBLIC_PLACEHOLDERS, (key, names - PUBLIC_PLACEHOLDERS)
    # The PRIVATE ceiling and client financials are never placeholders.
    assert not {"max_bp", "draft_amount", "balance", "program_fee"} & PUBLIC_PLACEHOLDERS


def test_reason_text_fills_public_facts_and_never_digits_from_private() -> None:
    action = Action(
        intent=Intent.COUNTER,
        facts={
            "counter_pct": Fact(
                id="counter_pct", kind="pct", value=4900, visibility="PUBLIC", source="engine"
            ),
            "secret": Fact(
                id="secret", kind="money", value=123456, visibility="PRIVATE", source="engine"
            ),
        },
        next_phase=Phase.NEGOTIATE,
        reason="bp=4900",
    )
    text = reason_text(action, _REF)
    assert "49%" in text
    assert "1,234" not in text and "{" not in text


def test_reason_text_unknown_code_is_generic_not_error() -> None:
    action = Action(intent=Intent.CLOSE, next_phase=Phase.END, reason="brand_new_code")
    assert reason_text(action, _REF)


def test_reason_text_missing_placeholder_falls_back_to_plain_sentence() -> None:
    # COUNTER without its fact (should not happen) still yields a sentence, no braces.
    action = Action(intent=Intent.COUNTER, next_phase=Phase.NEGOTIATE, reason="bp=4900")
    text = reason_text(action, _REF)
    assert text and "{" not in text


@pytest.mark.slow
async def test_every_reason_code_in_100_seed_oracle_eval_has_text(tmp_path: Path) -> None:
    """Same run as the CI eval (``--nlu oracle --scenarios 100 --seed 7``)."""
    settings = _build_settings(profile="offline", nlg="template", nlu="oracle")
    seen: set[tuple[str, str | None]] = set()
    for sc in generate(100, 7):
        r = await run_one_scenario(
            sc, settings=settings, llm=None, sim_phrasing="template", audit_dir=tmp_path
        )
        assert r["status"] == "ok", r
        log = AuditLog(tmp_path / f"audit_{sc.id}.db")
        for call_id in {row["call_id"] for row in log.list_calls(limit=200)}:
            for row in log.for_call(call_id):
                if row["actor"] == "policy" and row["type"] == "decide":
                    seen.add((row["payload"]["intent"], row["payload"]["reason"]))
        log.close()
    assert len(seen) > 10
    missing = sorted((i, r) for i, r in seen if reason_key(Intent(i), r) not in REASON_TEXT)
    assert missing == []
