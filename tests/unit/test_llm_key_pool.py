"""Phase 27: provider API key pools (discovery, rotation, 429 / 401 handling).

Offline only (httpx.MockTransport). Keys are fake strings; each test checks
which key served a request through the ``Authorization`` header and the
``key_id`` label in the ``on_call`` meta. Also covers carry-over 21.3 (one
deadline per target covers cooldown waits, 5xx backoff and key switches) and
the shipped demo NLU fallback (21.4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm.call_audit import audit_llm_calls, llm_call_scope
from app.llm.client import LLMClient, LLMUnavailable, _TpmBudget, parse_route_entry
from app.store.audit import AuditLog


class _Tiny(BaseModel):
    ok: bool


K1, K2, K3 = "sk-pool-ONE-1111", "sk-pool-TWO-2222", "sk-pool-THREE-3333"
_MSG = [{"role": "user", "content": "x"}]

_YAML = """
profiles:
  demo:
    nlu: [primary/m, backup/m]
    stt: [primary/whisper, backup/whisper]
providers:
  primary:
    base_url: "https://primary.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
    json_mode: true
    {extra}
  backup:
    base_url: "https://backup.test/v1"
    key_env: MISTRAL_API_KEY
    rpm: 600
    json_mode: true
"""


def _ok(content: str = '{"ok": true}', tokens: int = 5) -> dict[str, Any]:
    return {
        "id": "x",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens},
    }


def _bearer(request: httpx.Request) -> str:
    return request.headers["authorization"].removeprefix("Bearer ")


def _settings(pool: dict[str, str], **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "groq_api_key": None,
        "mistral_api_key": "backup-key",
        "gemini_api_key": None,
        "llm_profile": "demo",
        "llm_cache": False,
        "llm_cache_path": "unused.db",
        "api_key_pool": pool,
    }
    base.update(kw)
    return Settings(**base)


def _client(
    tmp: Path,
    settings: Settings,
    primary: Any,
    backup: Any | None = None,
    *,
    extra: str = "",
) -> tuple[LLMClient, list[dict[str, Any]], dict[str, list[str]]]:
    path = tmp / "providers.yaml"
    path.write_text(_YAML.replace("{extra}", extra))
    seen: dict[str, list[str]] = {"primary": [], "backup": []}
    metas: list[dict[str, Any]] = []

    def wrap(name: str, handler: Any) -> httpx.MockTransport:
        def _h(request: httpx.Request) -> httpx.Response:
            seen[name].append(_bearer(request))
            return handler(request)

        return httpx.MockTransport(_h)

    def backup_ok(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "backup"})
        return httpx.Response(200, json=_ok())

    client = LLMClient(
        settings,
        providers_path=path,
        http_clients={
            "primary": httpx.AsyncClient(transport=wrap("primary", primary)),
            "backup": httpx.AsyncClient(transport=wrap("backup", backup or backup_ok)),
        },
        on_call=metas.append,
        skip_health_check=True,
    )
    return client, metas, seen


def _rate_limited(retry_after: str | None = "30") -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers, json={"error": {"message": "tpm"}})


# --- discovery ---------------------------------------------------------------


def test_discovery_single_unsuffixed_key() -> None:
    s = _settings({}, groq_api_key=K1)
    assert s.api_keys("GROQ_API_KEY") == [(0, K1)]


def test_discovery_suffixed_only_contiguous_from_one() -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2, "GROQ_API_KEY_4": K3}
    # _4 is not reached: discovery stops at the first gap.
    assert _settings(pool).api_keys("GROQ_API_KEY") == [(1, K1), (2, K2)]


def test_discovery_both_forms_deduplicated_in_order() -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2, "GROQ_API_KEY_3": K3}
    s = _settings(pool, groq_api_key=K1)
    assert s.api_keys("GROQ_API_KEY") == [(0, K1), (2, K2), (3, K3)]


def test_discovery_undeclared_provider_and_empty_values() -> None:
    pool = {"FOO_API_KEY": K1, "FOO_API_KEY_1": " ", "FOO_API_KEY_2": K2}
    # A blank _1 still counts as present for contiguity but is skipped.
    assert _settings(pool).api_keys("FOO_API_KEY") == [(0, K1), (2, K2)]


def test_discovery_reads_dotenv_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GROQ_API_KEY", "GROQ_API_KEY_1", "GROQ_API_KEY_2", "GROQ_API_KEY_3"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"GROQ_API_KEY={K1}\nGROQ_API_KEY_1={K2}\n")
    monkeypatch.setenv("GROQ_API_KEY_2", K3)
    s = Settings(_env_file=env)  # type: ignore[call-arg]
    assert s.api_keys("GROQ_API_KEY") == [(0, K1), (1, K2), (2, K3)]
    # Report only which fake key leaked, never the repr (it may hold real env keys).
    leaked = [k for k in (K1, K2, K3) if k in repr(s)]
    assert leaked == []
    assert all(k not in repr(_settings({}, groq_api_key=k)) for k in (K1,))


def test_single_unsuffixed_key_client_unchanged(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, _settings({}, groq_api_key=K1), lambda r: None)
    assert [k.label for k in client._keys["primary"]] == ["primary#0"]
    assert "primary" in client._openai


# --- rotation ----------------------------------------------------------------


async def test_round_robin_across_keys(tmp_path: Path) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2, "GROQ_API_KEY_3": K3}
    client, metas, seen = _client(
        tmp_path, _settings(pool), lambda r: httpx.Response(200, json=_ok())
    )
    for _ in range(4):
        await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    assert seen["primary"] == [K1, K2, K3, K1]
    assert [m["key_id"] for m in metas] == ["primary#1", "primary#2", "primary#3", "primary#1"]
    # Each key gets its own rpm bucket.
    assert {k for k in client._limiters} == {("primary", i, "m") for i in (1, 2, 3)}


async def test_tpm_budget_prefers_key_with_tokens(tmp_path: Path) -> None:
    """Usage reconcile drains key 1's TPM budget, so the next pick skips it."""
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def primary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(tokens=6000 if _bearer(request) == K1 else 1))

    client, _, seen = _client(tmp_path, _settings(pool), primary, extra="tpm: 6000")
    for _ in range(3):
        await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    assert seen["primary"] == [K1, K2, K2]


async def test_tpm_budget_waits_when_empty() -> None:
    budget = _TpmBudget(6000)  # 100 tokens/s
    await budget.acquire(6000)
    t0 = time.perf_counter()
    await budget.acquire(20)
    assert 0.15 <= time.perf_counter() - t0 < 0.5


# --- rate limits and auth ----------------------------------------------------


async def test_429_moves_to_next_key_same_provider(tmp_path: Path) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def primary(request: httpx.Request) -> httpx.Response:
        return _rate_limited("30") if _bearer(request) == K1 else httpx.Response(200, json=_ok())

    client, metas, seen = _client(tmp_path, _settings(pool), primary)
    out = await client.chat_json("nlu", _MSG, _Tiny)
    # Key 1 is cooling, so the next call goes straight to key 2.
    await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    assert out.ok
    assert seen["primary"] == [K1, K2, K2]
    assert seen["backup"] == []
    assert metas[0]["key_id"] == "primary#1" and "429" in metas[0]["error"]
    assert metas[1]["key_id"] == "primary#2" and metas[1]["error"] is None
    assert metas[1]["failover_from"] is None


async def test_429_without_retry_after_uses_settings_cooldown(tmp_path: Path) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def primary(request: httpx.Request) -> httpx.Response:
        return _rate_limited(None) if _bearer(request) == K1 else httpx.Response(200, json=_ok())

    client, _, _ = _client(tmp_path, _settings(pool, llm_key_cooldown_s=42.0), primary)
    await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    key1 = client._keys["primary"][0]
    assert 40.0 < client._cooling_s(key1, "m") <= 42.0


async def test_all_keys_cooling_fails_over_to_next_route(tmp_path: Path) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}
    client, metas, seen = _client(tmp_path, _settings(pool), lambda r: _rate_limited("60"))
    out = await client.chat_json("nlu", _MSG, _Tiny)
    # Both keys cooling for 60 s: the next call skips primary without a request.
    await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    assert out.ok
    assert seen["primary"] == [K1, K2]
    assert len(seen["backup"]) == 2
    failed = [m for m in metas if m["error"]]
    assert [m["key_id"] for m in failed] == ["primary#1", "primary#2"]
    assert "every key" in failed[-1]["error"]
    assert metas[2]["provider"] == "backup" and metas[2]["failover_from"] == "primary/m"
    assert metas[3]["provider"] == "backup" and metas[3]["failover_from"] is None


async def test_401_disables_key_for_process_and_logs_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def primary(request: httpx.Request) -> httpx.Response:
        if _bearer(request) == K1:
            return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
        return httpx.Response(200, json=_ok())

    client, metas, seen = _client(tmp_path, _settings(pool), primary)
    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        for _ in range(3):
            assert (await client.chat_json("nlu", _MSG, _Tiny)).ok
    await client.aclose()
    assert seen["primary"] == [K1, K2, K2, K2]
    assert client._keys["primary"][0].disabled
    assert metas[0]["key_id"] == "primary#1" and "401" in metas[0]["error"]
    warnings = [r for r in caplog.records if "disabled" in r.getMessage()]
    assert len(warnings) == 1 and "primary#1" in warnings[0].getMessage()


async def test_every_key_disabled_fails_over(tmp_path: Path) -> None:
    client, metas, seen = _client(
        tmp_path,
        _settings({}, groq_api_key=K1),
        lambda r: httpx.Response(403, json={"error": {"message": "forbidden"}}),
    )
    assert (await client.chat_json("nlu", _MSG, _Tiny)).ok
    assert (await client.chat_json("nlu", _MSG, _Tiny)).ok
    await client.aclose()
    assert seen["primary"] == [K1]
    assert "every key disabled" in metas[0]["error"] and metas[0]["key_id"] == "primary#0"


async def test_stt_rotates_to_next_key_on_429(tmp_path: Path) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def primary(request: httpx.Request) -> httpx.Response:
        if _bearer(request) == K1:
            return _rate_limited("30")
        return httpx.Response(200, json={"text": "hello"})

    client, metas, seen = _client(tmp_path, _settings(pool), primary)
    assert await client.transcribe(b"RIFF....") == "hello"
    await client.aclose()
    assert seen["primary"] == [K1, K2] and seen["backup"] == []
    assert [m["key_id"] for m in metas] == ["primary#1", "primary#2"]


# --- one deadline per target (21.3) ------------------------------------------


async def test_deadline_covers_short_429_waits(tmp_path: Path) -> None:
    """Short Retry-After waits used to sleep up to 3x outside the timeout."""
    client, metas, seen = _client(
        tmp_path,
        _settings({}, groq_api_key=K1, llm_timeout_nlu_s=0.5),
        lambda r: _rate_limited("0.3"),
    )
    t0 = time.perf_counter()
    assert (await client.chat_json("nlu", _MSG, _Tiny)).ok
    elapsed = time.perf_counter() - t0
    await client.aclose()
    # One 0.3 s wait fits the 0.5 s deadline; a second does not -> fail over.
    assert len(seen["primary"]) == 2 and len(seen["backup"]) == 1
    assert elapsed < 0.6
    assert metas[0]["queue_ms"] >= 290


async def test_deadline_covers_5xx_backoff_and_key_switches(tmp_path: Path) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def primary(request: httpx.Request) -> httpx.Response:
        if _bearer(request) == K1:
            return _rate_limited("30")
        return httpx.Response(503, json={"error": {"message": "busy"}})

    client, metas, seen = _client(tmp_path, _settings(pool, llm_timeout_nlu_s=0.4), primary)
    t0 = time.perf_counter()
    assert (await client.chat_json("nlu", _MSG, _Tiny)).ok
    elapsed = time.perf_counter() - t0
    await client.aclose()
    # 5xx backoff (>= 0.5 s) would run past 0.4 s; the deadline cuts it.
    assert elapsed < 0.8
    assert seen["primary"][0] == K1 and seen["primary"][1] == K2
    final = [m for m in metas if m["provider"] == "primary"][-1]
    assert "timed out after 0.4s" in final["error"] and final["key_id"] == "primary#2"


async def test_hung_key_times_out_within_deadline(tmp_path: Path) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json=_ok())

    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}
    client, _, seen = _client(tmp_path, _settings(pool, llm_timeout_nlu_s=0.2), hang)
    t0 = time.perf_counter()
    assert (await client.chat_json("nlu", _MSG, _Tiny)).ok
    assert time.perf_counter() - t0 < 1.0
    await client.aclose()
    assert seen["primary"] == [K1]


# --- safety ------------------------------------------------------------------


async def test_no_key_value_in_audit_log_or_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Provider error bodies that echo the key are redacted to its label."""
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def echo(request: httpx.Request) -> httpx.Response:
        key = _bearer(request)
        if key == K1:
            return httpx.Response(401, json={"error": {"message": f"Incorrect API key: {key}"}})
        return httpx.Response(429, json={"error": {"message": f"limit for {key}"}})

    audit = AuditLog(tmp_path / "audit.db")
    client, metas, _ = _client(tmp_path, _settings(pool), echo, backup=echo)
    client.on_call = audit_llm_calls(audit, then=metas.append)
    with caplog.at_level(logging.DEBUG), llm_call_scope("call-1"):
        with pytest.raises(LLMUnavailable) as exc:
            await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    rows = json.dumps(audit.for_call("call-1"), default=str)
    blob = "\n".join([rows, json.dumps(metas), caplog.text, str(exc.value), repr(exc.value)])
    for secret in (K1, K2, "backup-key"):
        assert secret not in blob
    assert "primary#1" in rows and "primary#2" in rows
    assert exc.value.__cause__ is None or K1 not in str(exc.value.__cause__)
    audit.close()


async def test_cache_key_does_not_depend_on_serving_key(tmp_path: Path) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}
    settings = _settings(pool, llm_cache=True, llm_cache_path=str(tmp_path / "c.db"))
    client, metas, seen = _client(tmp_path, settings, lambda r: httpx.Response(200, json=_ok()))
    await client.chat_json("nlu", _MSG, _Tiny)
    await client.chat_json("nlu", _MSG, _Tiny)  # cursor is on key 2 now
    await client.aclose()
    assert seen["primary"] == [K1]
    assert metas[1]["cache_hit"] is True and metas[1]["key_id"] is None
    rows = (tmp_path / "c.db").read_bytes()
    assert K1.encode() not in rows and b"primary#1" not in rows


# --- shipped config (21.4) ---------------------------------------------------


def test_shipped_demo_nlu_fallback_fits_role_timeout() -> None:
    import yaml

    data = yaml.safe_load(Path("config/providers.yaml").read_text())
    demo_nlu = [parse_route_entry(e) for e in data["profiles"]["demo"]["nlu"]]
    # Phase 41/43: budgeted Claude (Haiku 5.5) first; the free chain after it is unchanged.
    assert demo_nlu[0].spec == "anthropic/claude-haiku-5-5" and demo_nlu[0].budgeted
    assert demo_nlu[1].spec == "groq/openai/gpt-oss-120b"
    # Fallback must be a fast target, not Gemini (p50 11.4 s > 6 s timeout).
    assert demo_nlu[2].provider == "cerebras"
    assert data["providers"]["groq"]["tpm"] == 8000


# --- carry-over 27.4: daily-quota cooldown parsed from the error body ----------


def _quota(body: dict[str, Any], retry_after: str | None = None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers, json=body)


@pytest.mark.parametrize(
    ("body", "retry_after", "low", "high"),
    [
        # Groq tokens-per-day: trust its rolling-window delay.
        (
            {
                "error": {
                    "message": "Rate limit reached on tokens per day (TPD): "
                    "Limit 200000, Used 199990. Please try again in 7m12.5s."
                }
            },
            None,
            430.0,
            433.0,
        ),
        # Gemini per-day quota: the short retryDelay is not the reset time.
        (
            {
                "error": {
                    "message": "Quota exceeded for GenerateRequestsPerDayPerProjectPerModel",
                    "details": [{"retryDelay": "35s"}],
                }
            },
            None,
            3590.0,
            3601.0,
        ),
        # Per-day text, no delay anywhere: long floor.
        ({"error": {"message": "requests per day limit reached"}}, None, 3590.0, 3601.0),
        # Per-minute Gemini quota: its retryDelay is used.
        ({"error": {"message": "quota", "details": [{"retryDelay": "20s"}]}}, None, 18.0, 20.5),
        # A header still wins when it is longer than the body delay.
        ({"error": {"message": "Please try again in 2s."}}, "30", 28.0, 30.5),
    ],
)
async def test_quota_429_cooldown_from_error_body(
    tmp_path: Path, body: dict[str, Any], retry_after: str | None, low: float, high: float
) -> None:
    pool = {"GROQ_API_KEY_1": K1, "GROQ_API_KEY_2": K2}

    def primary(request: httpx.Request) -> httpx.Response:
        if _bearer(request) == K1:
            return _quota(body, retry_after)
        return httpx.Response(200, json=_ok())

    client, _, seen = _client(tmp_path, _settings(pool, llm_key_cooldown_s=60.0), primary)
    await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    key1 = client._keys["primary"][0]
    assert low < client._cooling_s(key1, "m") <= high
    # The call moved to the other key instead of waiting.
    assert seen["primary"] == [K1, K2]


async def test_daily_quota_moves_on_instead_of_retrying_each_minute(tmp_path: Path) -> None:
    """All keys on a per-day quota: fail over to the next route, no 60 s retry loop."""
    pool = {"GROQ_API_KEY_1": K1}

    def primary(request: httpx.Request) -> httpx.Response:
        return _quota({"error": {"message": "limit: requests per day"}})

    client, _, seen = _client(tmp_path, _settings(pool, llm_key_cooldown_s=0.05), primary)
    await client.chat_json("nlu", _MSG, _Tiny)
    await asyncio.sleep(0.1)  # longer than the default cooldown
    await client.chat_json("nlu", _MSG, _Tiny)
    await client.aclose()
    assert seen["primary"] == [K1]  # cooled for the day, not re-hit
    assert len(seen["backup"]) == 2


# --- carry-over 24a.1: dedicated ``agent`` role --------------------------------


def test_agent_role_timeout_and_nlu_fallback_route(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, _settings({}, llm_timeout_agent_s=17.0), lambda r: None)
    assert client._timeout_for("agent") == 17.0
    # The test profile has no agent route → the nlu route.
    assert [t.spec for t in client._route("agent")] == [t.spec for t in client._route("nlu")]


def test_shipped_eval_profile_has_free_tier_agent_route() -> None:
    import yaml

    data = yaml.safe_load(Path("config/providers.yaml").read_text(encoding="utf-8"))
    route = [parse_route_entry(e) for e in data["profiles"]["eval"]["agent"]]
    assert route, "eval profile needs an agent route"
    assert all(t.provider in {"groq", "cerebras", "gemini", "openrouter"} for t in route)
    assert route[0].spec != parse_route_entry(data["profiles"]["eval"]["nlu"][0]).spec
    assert "agent" not in data["profiles"]["demo"]
    assert Settings(api_key_pool={}).llm_timeout_agent_s >= 15.0


def test_shipped_groq_nlg_routes_use_low_reasoning_effort() -> None:
    """Carry-over 21.7: reasoning tokens must not use up the NLG template budget."""
    import yaml

    from app.agent.nlg import NLG_MAX_TOKENS

    data = yaml.safe_load(Path("config/providers.yaml").read_text(encoding="utf-8"))
    for profile in ("demo", "eval"):
        groq = [
            t
            for t in (parse_route_entry(e) for e in data["profiles"][profile]["nlg"])
            if t.provider == "groq"
        ]
        assert groq and all(t.params.get("reasoning_effort") == "low" for t in groq), profile
    assert NLG_MAX_TOKENS >= 800
