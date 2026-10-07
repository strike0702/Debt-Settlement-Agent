"""Regression for Phase 29: a local ``.env`` must not change test results.

CI has no ``.env``; developers do. ``tests/conftest.py`` makes every non-live
test see code defaults only. These tests write a ``.env`` into the cwd and
prove ``Settings()`` / ``get_settings()`` ignore it, and that no Settings
override env var survives into a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, get_settings
from tests.conftest import settings_env_vars

_DEFAULT_DISCLOSURE = Settings.model_fields["opening_disclosure"].default

_DOTENV = (
    'OPENING_DISCLOSURE="You are speaking with a leaked dotenv line."\n'
    "FIRM_NAME=Leaky Firm\n"
    "NLG_MODE=bank\n"
    "MAX_TURNS=3\n"
    "GROQ_API_KEY_1=gsk-not-a-real-key\n"
    "MISTRAL_API_KEY=not-a-real-key\n"
)


@pytest.fixture
def dotenv_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    path.write_text(_DOTENV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return path


def test_dotenv_in_cwd_is_ignored(dotenv_cwd: Path) -> None:
    for s in (Settings(), get_settings()):
        assert s.opening_disclosure == _DEFAULT_DISCLOSURE
        assert s.firm_name == "Synthetic Debt Relief"
        assert s.nlg_mode == "llm"
        assert s.max_turns == 24
        assert s.mistral_api_key is None
        assert s.api_key_pool == {}
        assert s.api_keys("GROQ_API_KEY") == []


def test_dotenv_fixture_is_not_vacuous(dotenv_cwd: Path) -> None:
    # The same file, passed explicitly, is read: the guard above is the fixture.
    s = Settings(_env_file=dotenv_cwd)
    assert s.opening_disclosure == "You are speaking with a leaked dotenv line."
    assert s.api_keys("GROQ_API_KEY") == [(1, "gsk-not-a-real-key")]


def test_no_settings_env_vars_reach_tests() -> None:
    assert settings_env_vars() == set()
    assert Settings().opening_disclosure == _DEFAULT_DISCLOSURE
