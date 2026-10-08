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


class _FakeJudgeClient:
    """Stands in for ``LLMClient``: records the call, answers like the judge would."""

    calls: list[dict] = []

    def __init__(self, settings: Settings, **kwargs: object) -> None:
        self.settings = settings
        self._openai = {"anthropic": object()} if settings.anthropic_api_key else {}

    async def chat_text(self, role: str, messages: list, max_tokens: int, **kw: object) -> str:
        type(self).calls.append(
            {"role": role, "max_tokens": max_tokens, "profile": self.settings.llm_profile,
             "cache": self.settings.llm_cache, **kw}
        )
        return '{"pong": "ok"}'

    async def aclose(self) -> None:
        return None


def test_judge_target_skips_without_anthropic_key(monkeypatch) -> None:
    """[30.2] No ``ANTHROPIC_API_KEY`` → a SKIP line and no client is built."""
    import asyncio

    smoke = _smoke()
    _FakeJudgeClient.calls = []
    monkeypatch.setattr(smoke, "LLMClient", _FakeJudgeClient)
    lines = asyncio.run(smoke._smoke_judge(Settings(anthropic_api_key=None, api_key_pool={})))
    assert lines == [
        f"SKIP  judge/{smoke.JUDGE_PROFILE}  (no ANTHROPIC_API_KEY or ANTHROPIC_API_KEY_N)"
    ]
    assert _FakeJudgeClient.calls == []


def test_judge_target_calls_the_judge_role_once_per_key(monkeypatch) -> None:
    """[30.2] With a key: one ``chat_text("judge", ..., json_mode=True)``, eval, no cache."""
    import asyncio

    smoke = _smoke()
    _FakeJudgeClient.calls = []
    monkeypatch.setattr(smoke, "LLMClient", _FakeJudgeClient)
    lines = asyncio.run(smoke._smoke_judge(Settings(anthropic_api_key="a0", api_key_pool={})))
    assert len(lines) == 1 and lines[0].startswith("OK    judge/eval  [ANTHROPIC_API_KEY]")
    assert _FakeJudgeClient.calls == [
        {"role": "judge", "max_tokens": smoke.JUDGE_MAX_TOKENS, "profile": "eval",
         "cache": False, "json_mode": True}
    ]
