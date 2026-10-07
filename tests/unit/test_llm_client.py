"""Offline unit tests for the role-based LLM client (httpx.MockTransport)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm.client import FakeLLM, LLMClient, LLMUnavailable, strip_json_fences


class _Tiny(BaseModel):
    ok: bool


_PROVIDERS_YAML = """
profiles:
  demo:
    nlu: [primary/test-model, backup/test-model]
    nlg: [primary/test-model]
    stt: [primary/whisper]
  offline:
    all: [fake]
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
    min_interval_s: 0.12
    json_mode: true
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
  gemini:
    base_url: "https://gemini.test/v1"
    key_env: GEMINI_API_KEY
    rpm: 600
    json_mode: true
    quota_as_400: true
"""

_GEMINI_ROUTE_YAML = """
profiles:
  demo:
    nlu: [gemini/flash, backup/test-model]
providers:
  gemini:
    base_url: "https://gemini.test/v1"
    key_env: GEMINI_API_KEY
    rpm: 600
    json_mode: true
    quota_as_400: true
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
"""

_MISSING_KEY_YAML = """
profiles:
  demo:
    nlu: [primary/test-model, backup/test-model]
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
    json_mode: true
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
"""

_UNAVAIL_YAML = """
profiles:
  demo:
    nlu: [primary/test-model, backup/test-model]
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
    json_mode: true
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
"""


def _ok_body(content: str = '{"ok": true}') -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def _write_yaml(tmp: Path, text: str) -> Path:
    p = tmp / "providers.yaml"
    p.write_text(text)
    return p


def _settings(**kwargs: Any) -> Settings:
    base = {
        "groq_api_key": "pk",
        "mistral_api_key": "mk",
        "gemini_api_key": "gk",
        "llm_profile": "demo",
        "llm_cache": False,
        "llm_cache_path": "unused.db",
        # Hermetic: ignore GROQ_API_KEY_1.. etc. from the developer's env / .env.
        "api_key_pool": {},
    }
    base.update(kwargs)
    return Settings(**base)


def _client(
    yaml_path: Path,
    handlers: dict[str, Any],
    settings: Settings,
) -> tuple[LLMClient, dict[str, list[httpx.Request]]]:
    seen: dict[str, list[httpx.Request]] = {k: [] for k in handlers}

    def make_transport(name: str, handler: Any) -> httpx.MockTransport:
        def _wrap(request: httpx.Request) -> httpx.Response:
            seen[name].append(request)
            return handler(request)

        return httpx.MockTransport(_wrap)

    http_clients = {
        name: httpx.AsyncClient(transport=make_transport(name, handler))
        for name, handler in handlers.items()
    }
    client = LLMClient(
        settings,
        providers_path=yaml_path,
        http_clients=http_clients,
        skip_health_check=True,
    )
    return client, seen


@pytest.mark.asyncio
async def test_limiter_min_interval(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)
    calls = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_ok_body())

    settings = _settings()
    client, _ = _client(path, {"primary": primary, "backup": primary}, settings)
    msgs = [{"role": "user", "content": "hi"}]
    t0 = time.perf_counter()
    await client.chat_json("nlu", msgs, _Tiny)
    await client.chat_json("nlu", msgs, _Tiny)
    elapsed = time.perf_counter() - t0
    await client.aclose()
    assert calls["n"] == 2
    assert elapsed >= 0.12


@pytest.mark.asyncio
async def test_429_short_retry_same_target(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)
    n = {"i": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "0.05"},
                json={"error": {"message": "rate"}},
            )
        return httpx.Response(200, json=_ok_body())

    def backup(request: httpx.Request) -> httpx.Response:
        pytest.fail("backup should not be used on short retry-after")

    settings = _settings()
    client, seen = _client(path, {"primary": primary, "backup": backup}, settings)
    out = await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()
    assert out.ok is True
    assert n["i"] == 2
    assert len(seen["backup"]) == 0


@pytest.mark.asyncio
async def test_429_short_retry_capped_then_failover(tmp_path: Path) -> None:
    """Persistent short Retry-After must not loop forever — fail over after cap."""
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)
    n = {"i": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(
            429,
            headers={"retry-after": "0.01"},
            json={"error": {"message": "rate"}},
        )

    def backup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body('{"ok": true}'))

    events: list[dict[str, Any]] = []

    async def on_call(meta: dict[str, Any]) -> None:
        events.append(meta)

    settings = _settings()
    http_clients = {
        "primary": httpx.AsyncClient(transport=httpx.MockTransport(primary)),
        "backup": httpx.AsyncClient(transport=httpx.MockTransport(backup)),
    }
    client = LLMClient(
        settings,
        providers_path=path,
        http_clients=http_clients,
        on_call=on_call,
        skip_health_check=True,
    )
    out = await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()
    assert out.ok is True
    # 1 initial + 3 short retries = 4 attempts on primary, then backup.
    assert n["i"] == 4
    assert events[-1]["provider"] == "backup"
    assert events[-1]["failover_from"] == "primary/test-model"


@pytest.mark.asyncio
async def test_429_long_failover(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)

    def primary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "60"},
            json={"error": {"message": "daily"}},
        )

    def backup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body('{"ok": true}'))

    events: list[dict[str, Any]] = []

    async def on_call(meta: dict[str, Any]) -> None:
        events.append(meta)

    settings = _settings()
    http_clients = {
        "primary": httpx.AsyncClient(transport=httpx.MockTransport(primary)),
        "backup": httpx.AsyncClient(transport=httpx.MockTransport(backup)),
    }
    client = LLMClient(
        settings,
        providers_path=path,
        http_clients=http_clients,
        on_call=on_call,
        skip_health_check=True,
    )
    out = await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()
    assert out.ok is True
    assert events[-1]["provider"] == "backup"
    assert events[-1]["failover_from"] == "primary/test-model"


@pytest.mark.asyncio
async def test_gemini_400_quota_mapped_to_429(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _GEMINI_ROUTE_YAML)

    def gemini(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "RESOURCE_EXHAUSTED: Quota exceeded for metric",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

    def backup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    settings = _settings()
    client, _ = _client(path, {"gemini": gemini, "backup": backup}, settings)
    out = await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()
    assert out.ok is True


@pytest.mark.asyncio
async def test_cache_hit_second_identical_call(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)
    n = {"i": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(200, json=_ok_body())

    cache_path = tmp_path / "cache.db"
    settings = _settings(llm_cache=True, llm_cache_path=str(cache_path))
    events: list[dict[str, Any]] = []

    def on_call(meta: dict[str, Any]) -> None:
        events.append(meta)

    http_clients = {
        "primary": httpx.AsyncClient(transport=httpx.MockTransport(primary)),
        "backup": httpx.AsyncClient(transport=httpx.MockTransport(primary)),
    }
    client = LLMClient(
        settings,
        providers_path=path,
        http_clients=http_clients,
        on_call=on_call,
        skip_health_check=True,
    )
    msgs = [{"role": "user", "content": "same"}]
    await client.chat_json("nlu", msgs, _Tiny)
    await client.chat_json("nlu", msgs, _Tiny)
    await client.aclose()
    assert n["i"] == 1
    assert events[0]["cache_hit"] is False
    assert events[1]["cache_hit"] is True


@pytest.mark.asyncio
async def test_skip_missing_key(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _MISSING_KEY_YAML)

    def backup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    def primary(request: httpx.Request) -> httpx.Response:
        pytest.fail("primary has no key and must be skipped")

    settings = _settings(groq_api_key=None)
    # only backup gets a client; primary omitted because no key at init
    http_clients = {
        "backup": httpx.AsyncClient(transport=httpx.MockTransport(backup)),
        "primary": httpx.AsyncClient(transport=httpx.MockTransport(primary)),
    }
    client = LLMClient(
        settings,
        providers_path=path,
        http_clients=http_clients,
        skip_health_check=True,
    )
    assert "primary" not in client._openai
    out = await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()
    assert out.ok is True


@pytest.mark.asyncio
async def test_llm_unavailable_when_all_exhausted(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _UNAVAIL_YAML)

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "120"},
            json={"error": {"message": "quota"}},
        )

    settings = _settings()
    client, _ = _client(path, {"primary": boom, "backup": boom}, settings)
    with pytest.raises(LLMUnavailable):
        await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()


@pytest.mark.asyncio
async def test_fake_llm_queue() -> None:
    fake = FakeLLM()
    fake.enqueue("nlu", {"ok": True})
    out = await fake.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    assert out.ok is True
    with pytest.raises(LLMUnavailable):
        await fake.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)


def test_strip_json_fences() -> None:
    assert strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_json_fences('{"a": 1}') == '{"a": 1}'


@pytest.mark.asyncio
async def test_hung_provider_times_out_and_fails_over(tmp_path: Path) -> None:
    """F2: a provider that never answers fails over within the role timeout."""
    import asyncio

    path = _write_yaml(tmp_path, _PROVIDERS_YAML)

    async def primary(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=_ok_body())

    def backup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    events: list[dict[str, Any]] = []
    client = LLMClient(
        _settings(llm_timeout_nlu_s=0.2),
        providers_path=path,
        http_clients={
            "primary": httpx.AsyncClient(transport=httpx.MockTransport(primary)),
            "backup": httpx.AsyncClient(transport=httpx.MockTransport(backup)),
        },
        on_call=events.append,
        skip_health_check=True,
    )
    t0 = time.perf_counter()
    out = await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    elapsed = time.perf_counter() - t0
    await client.aclose()
    assert out.ok is True
    assert elapsed < 2.0
    failed, ok = events
    assert failed["provider"] == "primary"
    assert "timed out" in failed["error"]
    assert failed["latency_ms"] >= 200
    assert ok["provider"] == "backup"
    assert ok["failover_from"] == "primary/test-model"
    assert ok["error"] is None


def test_timeouts_are_per_role() -> None:
    s = Settings()
    assert (s.llm_timeout_nlu_s, s.llm_timeout_nlg_s, s.llm_timeout_stt_s) == (6.0, 4.0, 8.0)


@pytest.mark.asyncio
async def test_programming_error_does_not_fail_over(tmp_path: Path) -> None:
    """F17: only provider/HTTP/timeout errors fail over; a bug propagates."""
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)

    def backup(request: httpx.Request) -> httpx.Response:
        pytest.fail("backup must not be tried after a programming error")

    client, _ = _client(path, {"primary": backup, "backup": backup}, _settings())

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("bug in request building")

    client._call_chat = boom  # type: ignore[method-assign]
    with pytest.raises(TypeError):
        await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()


@pytest.mark.asyncio
async def test_connection_error_fails_over(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)

    def primary(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def backup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    client, seen = _client(path, {"primary": primary, "backup": backup}, _settings())
    out = await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()
    assert out.ok is True
    assert len(seen["backup"]) == 1


@pytest.mark.asyncio
async def test_hung_stt_times_out(tmp_path: Path) -> None:
    import asyncio

    path = _write_yaml(tmp_path, _PROVIDERS_YAML)

    async def primary(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json={"text": "late"})

    events: list[dict[str, Any]] = []
    client = LLMClient(
        _settings(llm_timeout_stt_s=0.2),
        providers_path=path,
        http_clients={"primary": httpx.AsyncClient(transport=httpx.MockTransport(primary))},
        on_call=events.append,
        skip_health_check=True,
    )
    t0 = time.perf_counter()
    with pytest.raises(LLMUnavailable):
        await client.transcribe(b"RIFF")
    await client.aclose()
    assert time.perf_counter() - t0 < 2.0
    assert events[0]["role"] == "stt" and events[0]["error"]


# --- Phase 21: F1 burst bucket, per-model buckets, route params, queue_ms ---


@pytest.mark.asyncio
async def test_bucket_burst_three_acquires_fast() -> None:
    """F1: a fresh rpm=25 bucket serves a turn's 3 calls without waiting."""
    from app.llm.client import _TokenBucket

    bucket = _TokenBucket(25)
    t0 = time.perf_counter()
    for _ in range(3):
        await bucket.acquire()
    assert time.perf_counter() - t0 < 0.1


@pytest.mark.asyncio
async def test_bucket_sustained_rate_respects_rpm() -> None:
    """After the burst of 5, tokens refill at rpm/60 per second."""
    from app.llm.client import _TokenBucket

    bucket = _TokenBucket(600)  # 10/s, burst 5
    t0 = time.perf_counter()
    for _ in range(10):
        await bucket.acquire()
    elapsed = time.perf_counter() - t0
    # 5 burst + 5 refilled at 10/s ≈ 0.5 s; never faster than the rate allows.
    assert 0.45 <= elapsed < 1.0


def test_bucket_capacity_capped_and_small_rpm() -> None:
    from app.llm.client import _TokenBucket

    assert _TokenBucket(25)._capacity == 5.0
    assert _TokenBucket(4)._capacity == 4.0
    assert _TokenBucket(0.5)._capacity == 1.0


_TWO_MODEL_YAML = """
profiles:
  demo:
    nlu: [primary/big]
    nlg: [primary/small]
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 1
    json_mode: true
"""


@pytest.mark.asyncio
async def test_buckets_are_per_model(tmp_path: Path) -> None:
    """F1: rpm=1 per model — the nlg model does not wait for the nlu model's token."""
    path = _write_yaml(tmp_path, _TWO_MODEL_YAML)
    client, _ = _client(
        path, {"primary": lambda r: httpx.Response(200, json=_ok_body())}, _settings()
    )
    msgs = [{"role": "user", "content": "hi"}]
    t0 = time.perf_counter()
    await client.chat_json("nlu", msgs, _Tiny)
    await client.chat_text("nlg", msgs, 10)
    elapsed = time.perf_counter() - t0
    await client.aclose()
    assert elapsed < 0.5
    assert set(client._limiters) == {("primary", 0, "big"), ("primary", 0, "small")}


_PARAMS_YAML = """
profiles:
  demo:
    nlu:
      - {target: primary/openai/gpt-oss-120b, params: {reasoning_effort: low}}
      - backup/test-model
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
    json_mode: true
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
"""


def test_parse_route_entry_forms() -> None:
    from app.llm.client import RouteTarget, parse_route_entry

    assert parse_route_entry("groq/openai/gpt-oss-120b") == RouteTarget(
        "groq", "openai/gpt-oss-120b"
    )
    t = parse_route_entry(
        {"target": "groq/openai/gpt-oss-120b", "params": {"reasoning_effort": "low"}}
    )
    assert (t.provider, t.model, dict(t.params)) == (
        "groq",
        "openai/gpt-oss-120b",
        {"reasoning_effort": "low"},
    )
    assert t.spec == "groq/openai/gpt-oss-120b"
    with pytest.raises(ValueError):
        parse_route_entry({"params": {}})
    with pytest.raises(ValueError):
        parse_route_entry({"target": "a/b", "parms": {}})


@pytest.mark.asyncio
async def test_route_params_sent_in_body(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _PARAMS_YAML)
    client, seen = _client(
        path,
        {
            "primary": lambda r: httpx.Response(200, json=_ok_body()),
            "backup": lambda r: httpx.Response(200, json=_ok_body()),
        },
        _settings(),
    )
    await client.chat_json("nlu", [{"role": "user", "content": "hi"}], _Tiny)
    await client.aclose()
    import json as _json

    body = _json.loads(seen["primary"][0].content)
    assert body["reasoning_effort"] == "low"
    assert body["model"] == "openai/gpt-oss-120b"


def test_cache_key_includes_params_and_keeps_plain_keys() -> None:
    from app.llm.client import _cache_key

    msgs = [{"role": "user", "content": "hi"}]
    plain = _cache_key("groq", "m", msgs, 0.0, 10)
    assert _cache_key("groq", "m", msgs, 0.0, 10, {}) == plain
    assert _cache_key("groq", "m", msgs, 0.0, 10, {"reasoning_effort": "low"}) != plain


@pytest.mark.asyncio
async def test_queue_ms_in_meta_and_scope(tmp_path: Path) -> None:
    """queue_ms (limiter wait) is in every meta and summed by queue_wait_scope."""
    from app.llm.client import queue_wait_scope

    path = _write_yaml(tmp_path, _PROVIDERS_YAML)  # primary min_interval_s=0.12
    metas: list[dict[str, Any]] = []
    client, _ = _client(
        path,
        {
            "primary": lambda r: httpx.Response(200, json=_ok_body()),
            "backup": lambda r: httpx.Response(200, json=_ok_body()),
        },
        _settings(),
    )
    client.on_call = metas.append
    msgs = [{"role": "user", "content": "hi"}]
    with queue_wait_scope() as acc:
        await client.chat_json("nlu", msgs, _Tiny)
        await client.chat_json("nlu", msgs, _Tiny)
    await client.aclose()
    assert all("queue_ms" in m for m in metas)
    assert metas[1]["queue_ms"] >= 100  # waited out min_interval_s
    assert acc.ms == pytest.approx(sum(m["queue_ms"] for m in metas))


_SLOW_BUCKET_YAML = """
profiles:
  demo:
    nlu: [primary/test-model, backup/test-model]
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 1
    json_mode: true
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
"""


@pytest.mark.asyncio
async def test_limiter_wait_bounded_by_role_timeout(tmp_path: Path) -> None:
    """[20.2] An empty bucket cannot stall past the role timeout; it fails over."""
    path = _write_yaml(tmp_path, _SLOW_BUCKET_YAML)
    metas: list[dict[str, Any]] = []
    client, seen = _client(
        path,
        {
            "primary": lambda r: httpx.Response(200, json=_ok_body()),
            "backup": lambda r: httpx.Response(200, json=_ok_body()),
        },
        _settings(llm_timeout_nlu_s=0.2),
    )
    client.on_call = metas.append
    msgs = [{"role": "user", "content": "hi"}]
    await client.chat_json("nlu", msgs, _Tiny)  # uses primary's only token
    t0 = time.perf_counter()
    await client.chat_json("nlu", msgs, _Tiny)  # primary needs 60 s → timeout → backup
    elapsed = time.perf_counter() - t0
    await client.aclose()
    assert elapsed < 1.0
    assert len(seen["primary"]) == 1 and len(seen["backup"]) == 1
    failed = [m for m in metas if m["error"]]
    assert len(failed) == 1 and failed[0]["queue_ms"] >= 150


def test_route_timeout_override_parses_and_validates() -> None:
    """[20.1] a route may carry its own timeout_s (eval Gemini NLU is slower than 6 s)."""
    from app.llm.client import parse_route_entry

    t = parse_route_entry({"target": "gemini/flash", "timeout_s": 20})
    assert t.timeout_s == 20.0 and t.params == {}
    assert parse_route_entry("gemini/flash").timeout_s is None
    for bad in (0, -1, "20", True):
        with pytest.raises(ValueError):
            parse_route_entry({"target": "gemini/flash", "timeout_s": bad})


_SLOW_TARGET_YAML = """
profiles:
  demo:
    nlu:
      - {target: primary/test-model, timeout_s: 1.0}
      - backup/test-model
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
    json_mode: true
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
"""


@pytest.mark.asyncio
async def test_route_timeout_overrides_role_timeout(tmp_path: Path) -> None:
    """A 0.4 s answer succeeds on a route with timeout_s=1 though the role timeout is 0.2 s."""
    import asyncio as _asyncio

    path = _write_yaml(tmp_path, _SLOW_TARGET_YAML)

    async def slow(request: httpx.Request) -> httpx.Response:
        await _asyncio.sleep(0.4)
        return httpx.Response(200, json=_ok_body())

    seen: dict[str, int] = {"backup": 0}

    def backup(request: httpx.Request) -> httpx.Response:
        seen["backup"] += 1
        return httpx.Response(200, json=_ok_body())

    client = LLMClient(
        _settings(llm_timeout_nlu_s=0.2),
        providers_path=path,
        http_clients={
            "primary": httpx.AsyncClient(transport=httpx.MockTransport(slow)),
            "backup": httpx.AsyncClient(transport=httpx.MockTransport(backup)),
        },
        skip_health_check=True,
    )
    out = await client.chat_json("nlu", [{"role": "user", "content": "hi"}], _Tiny)
    await client.aclose()
    assert out.ok and seen["backup"] == 0


def test_shipped_providers_yaml_parses() -> None:
    """Every profile route in config/providers.yaml parses (plain and mapping forms)."""
    client = LLMClient(_settings(llm_profile="offline"), skip_health_check=True)
    eval_nlu = client._profiles["eval"]["nlu"]
    assert eval_nlu[0].provider == "gemini" and eval_nlu[0].timeout_s == 20.0


def test_shipped_cerebras_limits_match_free_tier_docs() -> None:
    """[P27] Cerebras free tier for gpt-oss-120b is 5 RPM / 30K uncached TPM (docs)."""
    client = LLMClient(_settings(llm_profile="offline"), skip_health_check=True)
    cfg = client._providers["cerebras"]
    assert (cfg.rpm, cfg.tpm) == (5, 30000)


@pytest.mark.asyncio
async def test_short_429_retry_sleep_counts_as_queue(tmp_path: Path) -> None:
    """A Retry-After sleep is rate-limit wait: it shows up in queue_ms, not hidden."""
    path = _write_yaml(tmp_path, _PROVIDERS_YAML)
    n = {"i": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(
                429, headers={"retry-after": "0.2"}, json={"error": {"message": "rate"}}
            )
        return httpx.Response(200, json=_ok_body())

    metas: list[dict[str, Any]] = []
    client, _ = _client(path, {"primary": primary, "backup": primary}, _settings())
    client.on_call = metas.append
    await client.chat_json("nlu", [{"role": "user", "content": "x"}], _Tiny)
    await client.aclose()
    assert len(metas) == 1 and metas[0]["error"] is None
    assert metas[0]["queue_ms"] >= 190


@pytest.mark.asyncio
async def test_nlu_chat_text_sends_json_mode(tmp_path: Path) -> None:
    """P20/F16: production NLU (chat_text) asks json_mode providers for a JSON object."""
    import json as _json

    from app.agent.nlu import analyze

    path = _write_yaml(tmp_path, _PROVIDERS_YAML)
    reply = '{"stance": "other", "terms": []}'
    client, seen = _client(
        path,
        {
            "primary": lambda r: httpx.Response(200, json=_ok_body(reply)),
            "backup": lambda r: httpx.Response(200, json=_ok_body(reply)),
        },
        _settings(),
    )
    await client.chat_text("nlg", [{"role": "user", "content": "hi"}], 50)
    await analyze(
        "We could do forty percent.",
        "What can you do?",
        None,
        llm=client,
        settings=_settings(nlu_mode="llm"),
    )
    await client.aclose()
    plain, nlu = (_json.loads(r.content) for r in seen["primary"])
    assert "response_format" not in plain
    assert nlu["response_format"] == {"type": "json_object"}
