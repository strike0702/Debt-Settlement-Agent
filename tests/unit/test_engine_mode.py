"""Engine objective / fee policy settings through the adapter, and CONFIRM read-back.

Phase 59: ``ENGINE_OBJECTIVE`` (default ``fee_first`` since the 2026-10-27 decision) and
``ENGINE_FEE_POLICY`` (default ``front_loaded``) reach ``engine_adapter.evaluate`` /
``affordability``; the affordability cache key includes both; the validator's
``fee_proportional`` rule is off unless asked for; CONFIRM read-back renders every
engine shape and count correctly ("1 payment", never "1 payments"; the count spoken
equals the plan's).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from app.adapter import engine_adapter as ea
from app.adapter.validator import RULE_FEE_PROPORTIONAL, validate
from app.agent.nlg import render_action
from app.config import Settings, get_settings
from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact
from app.domain.scenario import load_scenario
from feasibility.engine import ScheduleRow
from feasibility.models import CreditorRules
from tests.engine import gen_cases
from tests.engine.golden.build_golden import scenario_case

ROOT = Path(__file__).resolve().parents[2]
SCEN = ROOT / "fixtures" / "scenarios"


def _true_rules(name: str) -> tuple[object, CreditorRules, date]:
    """(call, true ``sim.json`` rules, first payment date) as the golden corpus builds them."""
    call = load_scenario(SCEN / name)
    _, offer, rules = gen_cases.case_to_objects({**scenario_case(name), "settlement_bp": 100})
    return call, rules, offer.first_payment_date


def test_settings_defaults() -> None:
    s = Settings()
    assert (s.engine_objective, s.engine_fee_policy) == ("fee_first", "front_loaded")
    assert ea.engine_mode() == ("fee_first", "front_loaded")
    assert ea.engine_mode("fee_first", "proportional") == ("fee_first", "proportional")


def test_env_example_lists_the_engine_settings() -> None:
    text = (ROOT / ".env.example").read_text()
    assert "ENGINE_OBJECTIVE=fee_first" in text
    assert "ENGINE_FEE_POLICY=front_loaded" in text


def test_adapter_evaluate_follows_engine_objective_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call, rules, fpd = _true_rules("easy_deal")
    n = ea.evaluate(call, rules, 4000, fpd).facts.get("num_payments").value
    assert n == 2  # fee-first default: the take-home plan
    monkeypatch.setenv("ENGINE_OBJECTIVE", "client_first")
    get_settings.cache_clear()
    assert ea.evaluate(call, rules, 4000, fpd).facts.get("num_payments").value == 5
    assert ea.evaluate(call, rules, 4000, fpd, objective="fee_first").facts.get(
        "num_payments"
    ).value == 2


def test_affordability_cache_key_includes_mode() -> None:
    ea.clear_affordability_cache()
    call, rules, fpd = _true_rules("counter_ladder")
    a = ea.affordability(call, rules, fpd)
    b = ea.affordability(call, rules, fpd, objective="client_first")
    c = ea.affordability(call, rules, fpd, fee_policy="proportional")
    info = ea._affordability_cached.cache_info()
    assert info.misses == 3 and info.hits == 0
    assert a == b == c  # feasibility does not depend on the mode
    ea.affordability(call, rules, fpd)
    assert ea._affordability_cached.cache_info().hits == 1


def test_validator_fee_proportional_rule_only_when_on() -> None:
    call, rules, fpd = _true_rules("easy_deal")
    s = ea.evaluate(call, rules, 4000, fpd, objective="fee_first", fee_policy="front_loaded")
    args = (s.rows, call.client, s.offer_total_cents, s.program_fee_cents, rules, fpd)
    assert validate(*args) == []
    flagged = validate(*args, fee_policy="proportional")
    assert [v.rule for v in flagged] == [RULE_FEE_PROPORTIONAL]  # whole fee taken up front
    p = ea.evaluate(call, rules, 4000, fpd, fee_policy="proportional")
    assert validate(
        p.rows, call.client, p.offer_total_cents, p.program_fee_cents, rules, fpd,
        fee_policy="proportional",
    ) == []
    with pytest.raises(ValueError):
        validate(*args, fee_policy="back_loaded")


def test_fee_proportional_rule_counts_cumulative_share() -> None:
    call, rules, fpd = _true_rules("easy_deal")
    rows = [
        ScheduleRow(date(2026, 3, 31), 25000, 14400, rules.bank_fee_cents, 0),
        ScheduleRow(date(2026, 4, 30), 25000, 14401, rules.bank_fee_cents, 0),
    ]
    got = validate(rows, call.client, 50000, 28801, rules, fpd, fee_policy="proportional")
    assert RULE_FEE_PROPORTIONAL not in {v.rule for v in got}  # 14400 <= 28801/2
    rows[0] = ScheduleRow(date(2026, 3, 31), 25000, 14401, rules.bank_fee_cents, 0)
    rows[1] = ScheduleRow(date(2026, 4, 30), 25000, 14400, rules.bank_fee_cents, 0)
    got = validate(rows, call.client, 50000, 28801, rules, fpd, fee_policy="proportional")
    assert RULE_FEE_PROPORTIONAL in {v.rule for v in got}


# --- CONFIRM read-back for every shape and count ------------------------------------


def _bank_confirm_templates() -> list[str]:
    bank = json.loads((ROOT / "config" / "nlg_bank.json").read_text())
    return [
        t for e in bank["entries"] if e["intent"] == "CONFIRM_SCHEDULE" for t in e["templates"]
    ]


def test_confirm_readback_every_shape_and_count() -> None:
    """Real engine plans (7 scenarios, both objectives, every feasible 5%): the default
    template and every bank CONFIRM template say the right count, singular for one."""
    templates = [None, *_bank_confirm_templates()]
    seen: set[tuple[str, int]] = set()
    for name in sorted(p.name for p in SCEN.iterdir() if (p / "sim.json").exists()):
        call, rules, fpd = _true_rules(name)
        for objective in ("fee_first", "client_first"):
            for bp in range(500, 10001, 500):
                s = ea.evaluate(call, rules, bp, fpd, objective=objective)
                if not s.feasible:
                    continue
                pub = s.facts.public()
                n = pub["num_payments"].value
                seen.add((s.shape, n))
                facts = {
                    k: pub[k] for k in ("num_payments", "offer_total", "first_payment_date")
                }
                facts["settlement_pct"] = Fact(
                    id="settlement_pct", kind="pct", value=bp, visibility="PUBLIC",
                    source="engine",
                )
                for tpl in templates:
                    action = Action(
                        intent=Intent.CONFIRM_SCHEDULE, facts=facts, next_phase=Phase.CONFIRM,
                        template_override=tpl,
                    )
                    line = " ".join(render_action(action, call.client.as_of_date))
                    assert not re.search(r"\b1 (payments|installments)\b", line), line
                    m = re.search(r"\b(\d+) (payment|installment)s?\b", line)
                    assert m and int(m.group(1)) == n, (name, objective, bp, line)
    shapes = {shape for shape, _ in seen}
    assert shapes == {"even", "staircase", "balloon"}
    assert {n for _, n in seen} >= {1, 2}
    assert any(n > 2 for _, n in seen)
