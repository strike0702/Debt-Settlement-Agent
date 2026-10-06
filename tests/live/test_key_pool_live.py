"""Live key-pool sanity: one tiny chat call per configured key (free tier).

Skipped unless ``DSA_LIVE=1`` and at least one pool key is set. Calls each key's
client directly (no rotation, no failover, cache off) so every key is checked
on its own. Prints and asserts only labels and ok/fail; key values never
appear in output. Not part of the default run.
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.llm.client import LLMClient

# One cheap model per provider; Mistral / OpenRouter are not in the pool scope.
_MODELS = {
    "groq": "openai/gpt-oss-20b",
    "gemini": "gemini-3.1-flash-lite",
    "cerebras": "gpt-oss-120b",
}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("DSA_LIVE") != "1", reason="set DSA_LIVE=1"),
]


async def test_every_pool_key_answers_one_tiny_call() -> None:
    client = LLMClient(Settings(llm_cache=False), skip_health_check=True)
    keys = [k for p in _MODELS for k in client._keys.get(p, [])]
    if not keys:
        await client.aclose()
        pytest.skip("no provider keys configured")
    results: dict[str, str] = {}
    for key in keys:
        try:
            resp = await key.client.chat.completions.create(
                model=_MODELS[key.provider],
                messages=[{"role": "user", "content": "Reply with the word ok."}],
                max_tokens=64,
                timeout=20,
            )
            results[key.label] = "ok" if resp.choices else "fail: no choices"
        except Exception as e:  # report the class only; bodies may echo keys
            status = getattr(e, "status_code", None)
            results[key.label] = f"fail: {type(e).__name__}" + (f" {status}" if status else "")
    await client.aclose()
    print("\n" + "\n".join(f"{label}: {r}" for label, r in results.items()))
    assert all(r == "ok" for r in results.values()), results
