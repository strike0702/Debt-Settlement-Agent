#!/usr/bin/env python3
"""Live smoke: one tiny nlu-role JSON call per available provider.

Reads keys from ``.env`` via Settings. Skips providers without a key; with a
key pool (``GROQ_API_KEY_1``, ``_2``, ...) every key is smoked on its own.
Checks Ollama ``GET /api/tags`` before attempting a local call.
Prints provider, model, latency_ms (or SKIP / FAIL).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings  # noqa: E402
from app.llm.client import LLMClient, LLMUnavailable, parse_route_entry  # noqa: E402


class SmokeOut(BaseModel):
    pong: str


SMOKE_TARGETS = [
    ("groq", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    ("mistral", "mistral-small-latest", "MISTRAL_API_KEY"),
    ("gemini", "gemini-3.1-flash-lite", "GEMINI_API_KEY"),
    ("openrouter", "cohere/north-mini-code:free", "OPENROUTER_API_KEY"),
    ("cerebras", "gpt-oss-120b", "CEREBRAS_API_KEY"),
]


def _key_pool(settings: Settings, env_name: str) -> list[tuple[int, str]]:
    """Every configured key for ``env_name`` (unsuffixed and ``_N``), in pool order."""
    return settings.api_keys(env_name)


def _single_key_settings(settings: Settings, env_name: str, value: str) -> Settings:
    """Settings whose pool for ``env_name`` is exactly ``value`` (one key per smoke)."""
    pool = {
        k: v
        for k, v in settings.api_key_pool.items()
        if k != env_name and not k.startswith(f"{env_name}_")
    }
    update: dict[str, Any] = {"api_key_pool": pool}
    field = env_name.lower()
    if field in type(settings).model_fields:
        update[field] = value
    else:
        pool[env_name] = value
    return settings.model_copy(update=update)


def _ollama_model(model: str) -> str | None:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        if r.status_code != 200:
            return None
        names = set()
        for m in r.json().get("models") or []:
            n = m.get("name") or ""
            if n:
                names.add(n)
                names.add(n.split(":", 1)[0])
        if model in names or model.split(":", 1)[0] in names:
            return model
    except (httpx.HTTPError, OSError):
        return None
    return None


async def _one(provider: str, model: str, settings: Settings | None = None) -> tuple[str, float]:
    """Force a single-provider route via a throwaway YAML profile."""
    settings = (settings or get_settings()).model_copy()
    settings.llm_profile = "demo"
    settings.llm_cache = False

    # Build client then override route by temporary profile injection
    client = LLMClient(settings, skip_health_check=provider != "ollama")
    if provider not in client._openai:
        await client.aclose()
        raise LLMUnavailable(f"{provider} not initialized")
    target = [parse_route_entry(f"{provider}/{model}")]
    client._profiles["demo"] = {"nlu": target, "nlg": target, "sim": target, "stt": target}
    t0 = time.perf_counter()
    out = await client.chat_json(
        "nlu",
        [
            {"role": "system", "content": "Reply with JSON only."},
            {"role": "user", "content": 'Return {"pong":"ok"} as JSON.'},
        ],
        SmokeOut,
    )
    ms = (time.perf_counter() - t0) * 1000.0
    await client.aclose()
    if out.pong != "ok":
        raise RuntimeError(f"unexpected payload: {out!r}")
    return f"{provider}/{model}", ms


async def main() -> int:
    settings = get_settings()
    print(f"profile={settings.llm_profile} cache={settings.llm_cache}")
    results: list[str] = []

    for provider, model, key_env in SMOKE_TARGETS:
        keys = _key_pool(settings, key_env)
        if not keys:
            line = f"SKIP  {provider}/{model}  (no {key_env} or {key_env}_N)"
            print(line)
            results.append(line)
            continue
        for suffix, value in keys:
            key_label = key_env if suffix == 0 else f"{key_env}_{suffix}"
            try:
                label, ms = await _one(
                    provider, model, _single_key_settings(settings, key_env, value)
                )
                line = f"OK    {label}  [{key_label}]  {ms:.0f} ms"
            except Exception as e:
                line = f"FAIL  {provider}/{model}  [{key_label}]  {type(e).__name__}: {e}"
            print(line)
            results.append(line)

    ollama_model = "qwen3.5:9b"
    pulled = _ollama_model(ollama_model)
    if pulled is None:
        # try without tag match — still probe if daemon up
        try:
            r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
            up = r.status_code == 200
        except (httpx.HTTPError, OSError):
            up = False
        if not up:
            line = f"SKIP  ollama/{ollama_model}  (daemon down)"
        else:
            line = f"SKIP  ollama/{ollama_model}  (model not pulled)"
        print(line)
        results.append(line)
    else:
        try:
            label, ms = await _one("ollama", ollama_model)
            line = f"OK    {label}  {ms:.0f} ms"
            print(line)
            results.append(line)
        except Exception as e:
            line = f"FAIL  ollama/{ollama_model}  {type(e).__name__}: {e}"
            print(line)
            results.append(line)

    ok = any(r.startswith("OK") and r.split()[1].startswith("groq/") for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
