"""NLU retry exception scope (REVIEW F24)."""

from __future__ import annotations

from pathlib import Path


def test_analyze_does_not_swallow_unexpected_exceptions() -> None:
    """F24: broad except Exception removed from NLU retry loop."""
    src = Path("app/agent/nlu.py").read_text(encoding="utf-8")
    # Still import Exception for other uses? Prefer no bare continue-swallow.
    assert "except Exception as e:" not in src
