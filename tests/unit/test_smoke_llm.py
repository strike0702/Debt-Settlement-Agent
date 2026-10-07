"""[P27] ``scripts/smoke_llm.py`` sees suffixed-only key pools and smokes each key."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from app.config import Settings


def _smoke() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "smoke_llm.py"
    spec = importlib.util.spec_from_file_location("smoke_llm", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_suffixed_only_pool_is_present_and_each_key_isolated() -> None:
    smoke = _smoke()
    s = Settings(
        groq_api_key=None,
        api_key_pool={"GROQ_API_KEY_1": "g1", "GROQ_API_KEY_2": "g2", "MISTRAL_API_KEY": "m"},
    )
    assert smoke._key_pool(s, "GROQ_API_KEY") == [(1, "g1"), (2, "g2")]
    one = smoke._single_key_settings(s, "GROQ_API_KEY", "g2")
    assert one.api_keys("GROQ_API_KEY") == [(0, "g2")]
    assert one.api_keys("MISTRAL_API_KEY") == s.api_keys("MISTRAL_API_KEY")


def test_unsuffixed_field_key_is_kept() -> None:
    smoke = _smoke()
    s = Settings(groq_api_key="g0", api_key_pool={"GROQ_API_KEY_1": "g1"})
    assert [v for _, v in smoke._key_pool(s, "GROQ_API_KEY")] == ["g0", "g1"]
    one = smoke._single_key_settings(s, "GROQ_API_KEY", "g1")
    assert one.api_keys("GROQ_API_KEY") == [(0, "g1")]
