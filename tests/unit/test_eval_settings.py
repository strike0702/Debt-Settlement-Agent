"""Unit tests for eval runner settings (REVIEW F16–F17)."""

from __future__ import annotations

from app.config import Settings
from eval.run_eval import _build_settings


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
