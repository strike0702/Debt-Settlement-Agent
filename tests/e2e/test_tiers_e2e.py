"""Scenarios with non-empty ``min_payment_tiers`` through engine, validator, and speech.

Offline oracle NLU + template NLG + template sim. Every deal must validate
under the creditor's agreed rules (tiers included), and no agent line may
speak a raw tier structure.
"""

from __future__ import annotations

from pathlib import Path

from app.llm.client import make_client
from eval.run_eval import _build_settings, run_one_scenario
from sim.scenarios import generate

_SETTINGS = _build_settings(profile="offline", nlg="template", nlu="oracle")


async def test_tiered_scenarios_end_to_end(tmp_path: Path) -> None:
    tiered = [s for s in generate(100, 7) if s.true_rules.min_payment_tiers]
    assert tiered
    deals_all_fields = 0
    for sc in tiered:
        r = await run_one_scenario(
            sc,
            settings=_SETTINGS,
            llm=make_client(_SETTINGS),
            sim_phrasing="template",
            audit_dir=tmp_path,
        )
        assert r["status"] == "ok", sc.id
        for line in r["agent_lines"]:
            assert not any(ch in line for ch in "[](){}"), (sc.id, line)
        assert r["false_known_count"] == 0, sc.id
        if r["got_deal"]:
            assert r["agreement_valid"] is True, sc.id
            if r["rule_fields_correct"] == r["rule_fields_total"]:
                deals_all_fields += 1
    assert deals_all_fields > 0
