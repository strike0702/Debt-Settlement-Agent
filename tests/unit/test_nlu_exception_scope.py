"""NLU retry exception scope: a broad ``except`` must not hide real bugs."""

from __future__ import annotations

from pathlib import Path


def test_analyze_does_not_swallow_unexpected_exceptions() -> None:
    """No broad ``except Exception`` in the NLU package (the retry loop included)."""
    src = "".join(p.read_text(encoding="utf-8") for p in Path("app/agent/nlu").glob("*.py"))
    # The retry loop catches only parse / validation errors; anything else surfaces.
    assert "except Exception as e:" not in src
