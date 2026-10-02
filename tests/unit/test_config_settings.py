"""Settings cache (REVIEW F31)."""

from __future__ import annotations

from app.config import get_settings


def test_get_settings_cached() -> None:
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b
