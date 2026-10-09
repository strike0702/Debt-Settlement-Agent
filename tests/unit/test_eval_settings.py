"""Eval runner: settings, CLI layers, leak scan helpers, belief metrics (F16–F19)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.domain.belief import TermStatus
from app.domain.scenario import load_scenario
from app.llm.client import make_client
from eval.run_eval import (
    _belief_metrics,
    _build_settings,
    _count_leaks,
    _engine_private_values,
    main,
    run_one_scenario,
)
from sim.scenarios import TrueRules, generate_one


def test_build_settings_passes_policy_knobs() -> None:
    """F16 / Phase 45: eval must not drop policy knobs back to their defaults."""
    src = Settings(
        accept_line_pct_of_max_bp=8000, max_same_question=3, max_no_progress_turns=5,
        max_turns=24,
    )
    out = _build_settings(profile="offline", nlg="template", base=src)
    assert out.accept_line_pct_of_max_bp == 8000
    assert out.max_same_question == 3 and out.max_no_progress_turns == 5


def test_build_settings_preserves_max_turns() -> None:
    """F17: outer loop should follow settings.max_turns (not a hard-coded 30)."""
    src = Settings(max_turns=24)
    out = _build_settings(profile="offline", nlg="template", base=src)
    assert out.max_turns == 24


def test_belief_metrics_scores_all_true_rules_known_only() -> None:
    """F19: all TrueRules fields scored; ASSUMED match must not count as extracted."""
    demo = Path(__file__).resolve().parents[2] / "fixtures" / "demo"
    call = load_scenario(demo)
    tr = TrueRules(
        max_payments=6,
        min_payment_cents=10000,
        payment_structure="even",
        first_payment_date=date(2026, 4, 30),
        max_segments=2,
        max_token_pays=6,
        min_payment_tiers=(),
    )
    scenario = SimpleNamespace(true_rules=tr)
    # Real belief: ASSUMED seeds for optional fields.
    from app.domain.belief import BeliefState

    belief = BeliefState(call.client)
    session = MagicMock()
    session.belief = belief
    metrics = _belief_metrics(scenario, session)  # type: ignore[arg-type]
    assert metrics["rule_fields_total"] == 7
    assert belief.get("first_payment_date").status == TermStatus.ASSUMED
    # ASSUMED matching truth must not count toward rule_fields_correct.
    assert metrics["rule_fields_correct"] == 0


def test_build_settings_nlu_oracle() -> None:
    out = _build_settings(profile="offline", nlg="template", nlu="oracle", base=Settings())
    assert out.nlu_mode == "oracle"
    assert _build_settings(profile="eval", nlg="template", base=Settings()).nlu_mode == "llm"


@pytest.mark.parametrize(
    "argv",
    [
        ["--nlu", "oracle", "--nlg", "llm", "--sim-phrasing", "template"],
        ["--nlu", "oracle", "--nlg", "template", "--sim-phrasing", "llm"],
        ["--nlu", "oracle", "--nlg", "template", "--sim-phrasing", "template",
         "--no-oracle-overlay"],
    ],
)
def test_cli_oracle_rejects_networked_or_meaningless_flags(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_leak_scan_engine_private_exempts_spoken_public_facts() -> None:
    lines = ["We can propose 48% of the balance.", "Our max is 52%."]
    blocklist = {("pct", 4800), ("pct", 5200)}
    assert _count_leaks(lines, blocklist) == 2
    # 48% was a PUBLIC counter fact; only the unsanctioned 52% leaks.
    assert _count_leaks(lines, blocklist, exempt={("pct", 4800)}) == 1


def test_engine_private_values_include_max_bp_and_rescue() -> None:
    sc = generate_one("flexible", "rescue", seed=1001)
    events = [{"type": "affordability", "payload": {"max_bp": 3100}}]
    vals = _engine_private_values(sc, sc.true_rules, events)
    assert ("pct", 3100) in vals
    money = {v for k, v in vals if k == "money"}
    assert len(money) >= 1  # rescue lump / increment from the true-rules engine run


@pytest.mark.asyncio
async def test_run_one_scenario_oracle_offline(tmp_path: Path) -> None:
    """Oracle layer: FakeLLM, no network; 7 rule fields and quality keys per call."""
    settings = _build_settings(profile="offline", nlg="template", nlu="oracle", base=Settings())
    sc = generate_one("flexible", "deal", seed=101)
    result = await run_one_scenario(
        sc,
        settings=settings,
        llm=make_client(settings),
        sim_phrasing="template",
        audit_dir=tmp_path,
    )
    assert result["status"] == "ok"
    assert result["rule_fields_total"] == 7
    assert result["rule_fields_correct"] == 7
    for key in (
        "counters_spoken",
        "max_counters",
        "identical_consecutive_agent_moves",
        "turns_to_outcome",
        "hit_max_turns",
    ):
        assert key in result
    assert result["hit_max_turns"] is False


def test_cli_default_nlg_template() -> None:
    """F29: bare eval CLI defaults to template NLG (cheap smoke)."""
    from pathlib import Path

    import eval.run_eval as run_eval

    src = Path(run_eval.__file__).read_text(encoding="utf-8")
    assert 'default="template"' in src
