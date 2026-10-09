"""Settings cache (REVIEW F31)."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, get_settings

_ROOT = Path(__file__).resolve().parents[2]


def test_get_settings_cached() -> None:
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b


def test_max_counters_default_is_six() -> None:
    """Phase 46a: six spoken counters per call (holds included)."""
    assert Settings().max_counters == 6


def test_env_example_lists_live_policy_knobs_only() -> None:
    """``.env.example`` names real settings with their defaults; no retired keys (Phase 46a)."""
    lines = (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    pairs = dict(ln.split("=", 1) for ln in lines if ln and not ln.startswith("#") and "=" in ln)
    assert "CLOSE_GAP_BP" not in pairs
    defaults = Settings()
    for key in ("MAX_COUNTERS", "ACCEPT_LINE_PCT_OF_MAX_BP", "MAX_SAME_QUESTION",
                "MAX_NO_PROGRESS_TURNS"):
        assert int(pairs[key]) == getattr(defaults, key.lower()), key
    fields = {name.upper() for name in Settings.model_fields}
    unknown = {k for k in pairs if k not in fields and not k.endswith("_API_KEY")}
    assert not unknown, unknown
