"""Unit tests for eval runner settings / belief metrics (REVIEW F16–F19)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.domain.belief import TermStatus
from app.domain.scenario import load_scenario
from eval.run_eval import _belief_metrics, _build_settings
from sim.scenarios import TrueRules


def test_build_settings_passes_close_gap_bp() -> None:
    """F16: eval must not drop close_gap_bp back to the default 200."""
    src = Settings(close_gap_bp=500, max_turns=24)
    out = _build_settings(profile="offline", nlg="template", base=src)
    assert out.close_gap_bp == 500


def test_build_settings_preserves_max_turns() -> None:
    """F17: outer loop should follow settings.max_turns (not a hard-coded 30)."""
    src = Settings(max_turns=24, close_gap_bp=200)
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
