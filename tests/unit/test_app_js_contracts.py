"""Static contract checks for voice UI (REVIEW F06)."""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "app.js"


def test_tts_onerror_sends_sentence_done() -> None:
    """F06: utter.onerror must ack so server pending is not stuck."""
    src = APP_JS.read_text(encoding="utf-8")
    # Locate onerror handler body after utter.onend.
    idx = src.index("utter.onerror")
    chunk = src[idx : idx + 400]
    assert "sentence_done" in chunk
    assert "showNotice" in chunk
