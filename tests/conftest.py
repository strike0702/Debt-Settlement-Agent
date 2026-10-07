"""Shared pytest config: the ``live`` marker and hermetic ``Settings`` for every test.

``app.config.Settings`` reads ``.env`` from the cwd and the process environment.
A developer's local ``.env`` (e.g. a custom ``OPENING_DISCLOSURE``) used to leak
into goldens, so the suite passed locally and failed on CI (Phase 29). The
autouse ``_hermetic_settings`` fixture turns the dotenv source off and unsets
every env var ``Settings`` would read (declared fields, derived from
``Settings.model_fields``, plus ``*_KEY`` / ``*_KEY_<n>`` pool vars).

Live tests (``@pytest.mark.live`` or anything under ``tests/live/``) are exempt:
they need the real keys and opt in with ``DSA_LIVE=1``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import _POOL_VAR_RE, Settings, get_settings

_LIVE_DIR = Path(__file__).resolve().parent / "live"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: real provider calls; needs DSA_LIVE=1 and keys (skipped otherwise)"
    )


def settings_env_vars() -> set[str]:
    """Env var names ``Settings`` reads right now: its fields plus key-pool vars."""
    # No env_prefix and case-insensitive matching: field ``opening_disclosure``
    # is read from OPENING_DISCLOSURE (or any casing of it).
    fields = {name.upper() for name in Settings.model_fields}
    return {
        name
        for name in os.environ
        if name.upper() in fields or _POOL_VAR_RE.match(name.upper())
    }


def _is_live(request: pytest.FixtureRequest) -> bool:
    if request.node.get_closest_marker("live") is not None:
        return True
    return _LIVE_DIR in Path(str(request.node.path)).resolve().parents


@pytest.fixture(autouse=True)
def _hermetic_settings(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Make ``Settings()`` see only code defaults: no ``.env``, no override env vars."""
    if _is_live(request):
        yield
        return
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in settings_env_vars():
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
