#!/usr/bin/env python3
"""Live smoke: one tiny nlu-role JSON call per available provider.

Reads keys from ``.env`` via Settings. Skips providers without a key.
Checks Ollama ``GET /api/tags`` before attempting a local call.
Prints provider, model, latency_ms (or SKIP / FAIL).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.llm.client import LLMClient, LLMUnavailable  # noqa: E402


class SmokeOut(BaseModel):
    pong: str


SMOKE_TARGETS = [
    ("groq", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    ("mistral", "mistral-small-latest", "MISTRAL_API_KEY"),
    ("gemini", "gemini-3.1-flash-lite", "GEMINI_API_KEY"),
    ("openrouter", "cohere/north-mini-code:free", "OPENROUTER_API_KEY"),
    ("cerebras", "gpt-oss-120b", "CEREBRAS_API_KEY"),
]


def _key_present(settings, env_name: str) -> bool:
    attr = {
        "GROQ_API_KEY": "groq_api_key",
        "MISTRAL_API_KEY": "mistral_api_key",
        "GEMINI_API_KEY": "gemini_api_key",
        "OPENROUTER_API_KEY": "openrouter_api_key",
        "CEREBRAS_API_KEY": "cerebras_api_key",
    }[env_name]
    val = getattr(settings, attr, None)
    return bool(val and str(val).strip())


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


async def _one(provider: str, model: str) -> tuple[str, float]:
    """Force a single-provider route via a throwaway YAML profile."""
    settings = get_settings()
    settings.llm_profile = "demo"
    settings.llm_cache = False

    # Build client then override route by temporary profile injection
    client = LLMClient(settings, skip_health_check=provider != "ollama")
    if provider not in client._openai:
        await client.aclose()
        raise LLMUnavailable(f"{provider} not initialized")
    client._profiles["demo"] = {
        "nlu": [f"{provider}/{model}"],
        "nlg": [f"{provider}/{model}"],
        "sim": [f"{provider}/{model}"],
        "stt": [f"{provider}/{model}"],
    }
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
        if not _key_present(settings, key_env):
            line = f"SKIP  {provider}/{model}  (no {key_env})"
            print(line)
            results.append(line)
            continue
        try:
            label, ms = await _one(provider, model)
            line = f"OK    {label}  {ms:.0f} ms"
            print(line)
            results.append(line)
        except Exception as e:
            line = f"FAIL  {provider}/{model}  {type(e).__name__}: {e}"
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
