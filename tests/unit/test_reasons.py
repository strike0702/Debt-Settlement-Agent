"""REASON_TEXT covers every reason code ``decide()`` and the orchestrator emit."""

from __future__ import annotations

import re
import string
from datetime import date
from pathlib import Path

import pytest

from app.agent.reasons import (
    PUBLIC_PLACEHOLDERS,
    REASON_SHORT,
    REASON_TEXT,
    reason_key,
    reason_short,
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


def test_counter_without_offer_total_omits_the_amount() -> None:
    """Carry-over 22.5: no "(that value)" when the engine gave no offer total."""
    from app.domain.actions import Action, Phase
    from app.domain.facts import Fact

    pct = Fact(id="counter_pct", kind="pct", value=4900, visibility="PUBLIC", source="engine")
    bare = Action(
        intent=Intent.COUNTER,
        facts={"counter_pct": pct},
        next_phase=Phase.NEGOTIATE,
        reason="bp=4900",
    )
    text = reason_text(bare, date(2026, 3, 1))
    assert text.startswith("We offer 49%.") and "that value" not in text
    total = Fact(
        id="offer_total", kind="money", value=123_400, visibility="PUBLIC", source="engine"
    )
    full = bare.model_copy(update={"facts": {"counter_pct": pct, "offer_total": total}})
    assert reason_text(full, date(2026, 3, 1)).startswith("We offer 49% ($1,234")
    # Display variant only: the reason code is unchanged.
    assert reason_key(Intent.COUNTER, "bp=4900") == "counter"


# Phase 36 rewrote the sentences for newcomers. Text only: these are the placeholders
# each key had before the rewrite, so no fact was added, dropped or swapped.
_PLACEHOLDERS_BEFORE: dict[str, set[str]] = {
    "ask_field": {"field_label"},
    "read_back": {"field_label"},
    "clarify_field": {"field_label"},
    "counter": {"counter_pct", "offer_total"},
    "counter_no_total": {"counter_pct"},
    "alt_first_payment_date": {"alt_first_payment_date"},
    "alt_min_payment_cents": {"alt_min_payment_cents"},
    "alt_max_payments": {"alt_max_payments"},
    **{
        k: {"settlement_pct"}
        for k in (
            "confirm",
            "ask_within_offer",
            "rep_firm",
            "counters_exhausted",
            "no_lower_counter",
            "rep_held",
            "terms_revised",
        )
    },
    # Phase 45 ladder moves (COUNTER templates speak the counter).
    "hold": {"counter_pct"},
    "step": {"counter_pct"},
    "final_counter": {"counter_pct"},
}


def test_rewrite_kept_every_key_and_placeholder() -> None:
    fmt = string.Formatter()
    for key, text in REASON_TEXT.items():
        names = {n for _, n, _, _ in fmt.parse(text) if n}
        assert names == _PLACEHOLDERS_BEFORE.get(key, set()), key
    # +2 in Phase 39: amount_meaning(_unresolved). Phase 45: -2 (gap_small,
    # ladder_stalled), +7 (hold, step, final_counter, rep_held, above_accept_line,
    # repeated_question, no_progress).
    assert len(REASON_TEXT) == 52


_JARGON = ("ladder", "read back", "read-back", "wrap", "hedged", "engine", "policy", "feasible")


def test_sentences_are_plain_full_sentences() -> None:
    for key, text in REASON_TEXT.items():
        assert text[0].isupper() and text.endswith("."), key
        assert not any(j in text.lower() for j in _JARGON), (key, text)


def test_short_forms_cover_every_reason_and_carry_no_numbers() -> None:
    assert set(REASON_SHORT) == set(REASON_TEXT)
    for key, text in REASON_SHORT.items():
        assert text[0].isupper() and text.endswith("."), key
        assert len(text) <= 60, (key, len(text))
        assert not re.search(r"[0-9{}]", text), key
        assert not any(j in text.lower() for j in _JARGON), (key, text)
    assert reason_short(Intent.COUNTER, "bp=4900") == REASON_SHORT["counter"]
    assert reason_short(Intent.ASK, "max_payments") == REASON_SHORT["ask_field"]
    assert reason_short(Intent.CLOSE, "brand_new_code") is None
