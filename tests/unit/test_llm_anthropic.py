"""Phase 30: Anthropic provider (Messages API) and the ``judge`` role.

Offline only: the ``anthropic`` SDK client is built on ``httpx2``, so each test
hands ``LLMClient`` an ``anthropic.DefaultAsyncHttpxClient`` with an
``httpx2.MockTransport``. Keys are fake strings; no test needs a real key or
``.env``. Covers request shape (no thinking / tool_choice / temperature),
response parsing, token accounting in the audit, 429 key rotation, 529 / 5xx
retry then a loud failure (no free-tier fallback for the judge), and that a key
never reaches logs, metas, errors or audit rows.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient
from pydantic import BaseModel

from app.config import Settings
from app.llm.call_audit import audit_llm_calls, llm_call_scope
from app.llm.client import LLMClient, LLMUnavailable, parse_route_entry
from app.store.audit import AuditLog

K1, K2 = "sk-ant-test-ONE-1111", "sk-ant-test-TWO-2222"
_SYS = "You judge transcripts."
_MSG = [{"role": "system", "content": _SYS}, {"role": "user", "content": "Pick one."}]

_YAML = """
profiles:
  eval:
    nlu: [free/m]
    judge:
      - {target: anthropic/claude-sonnet-5-5, params: {output_config: {effort: low}}}
  demo:
    nlu: [free/m]
providers:
  anthropic:
    api: anthropic
    base_url: "https://api.anthropic.test"
    key_env: ANTHROPIC_API_KEY
    rpm: 600
  free:
    base_url: "https://free.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
"""


class _Tiny(BaseModel):
    ok: bool


def _message(text: str = '{"winner": "1"}', *, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }


def _error(status: int, etype: str, retry_after: str | None = None) -> httpx2.Response:
    headers = {"retry-after": retry_after} if retry_after else {}
    body = {"type": "error", "error": {"type": etype, "message": etype}}
    return httpx2.Response(status, headers=headers, json=body)


def _settings(pool: dict[str, str], **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "groq_api_key": "free-key",
        "mistral_api_key": None,
        "gemini_api_key": None,
        # Declared field wins for the unsuffixed name (Settings.api_keys).
        "anthropic_api_key": pool.pop("ANTHROPIC_API_KEY", None),
        "llm_profile": "eval",
        "llm_cache": False,
        "llm_cache_path": "unused.db",
        "api_key_pool": pool,
    }
    base.update(kw)
    return Settings(**base)


def _client(
    tmp: Path, settings: Settings, handler: Any
) -> tuple[LLMClient, list[dict[str, Any]], list[httpx2.Request], list[int]]:
    path = tmp / "providers.yaml"
    path.write_text(_YAML)
    seen: list[httpx2.Request] = []
    free_calls: list[int] = []

    def _h(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    def free(request: httpx.Request) -> httpx.Response:
        free_calls.append(1)
        return httpx.Response(200, json={"choices": []})

    metas: list[dict[str, Any]] = []
    client = LLMClient(
        settings,
        providers_path=path,
        http_clients={
            "anthropic": DefaultAsyncHttpxClient(transport=httpx2.MockTransport(_h)),
            "free": httpx.AsyncClient(transport=httpx.MockTransport(free)),
        },
        on_call=metas.append,
        skip_health_check=True,
    )
    return client, metas, seen, free_calls


async def test_request_shape_and_response_parsing(tmp_path: Path) -> None:
    client, metas, seen, _ = _client(
        tmp_path,
        _settings({"ANTHROPIC_API_KEY": K1}),
        lambda r: httpx2.Response(200, json=_message()),
    )
    out = await client.chat_text("judge", _MSG, max_tokens=400)
    await client.aclose()
    assert out == '{"winner": "1"}'  # thinking block skipped, text kept
    req = seen[0]
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == K1
    body = json.loads(req.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 400
    assert body["system"] == _SYS
    assert body["messages"] == [{"role": "user", "content": "Pick one."}]
    assert body["output_config"] == {"effort": "low"}
    for banned in ("thinking", "tool_choice", "temperature", "response_format"):
        assert banned not in body
    m = metas[0]
    assert (m["provider"], m["model"], m["error"]) == ("anthropic", "claude-sonnet-5-5", None)
    assert (m["prompt_tokens"], m["completion_tokens"]) == (123, 45)
    assert m["key_id"] == "anthropic#0" and m["latency_ms"] >= 0


async def test_json_mode_is_emulated_by_prompt_and_parsed_locally(tmp_path: Path) -> None:
    reply = '```json\n{"ok": true}\n```'
    client, _, seen, _ = _client(
        tmp_path,
        _settings({"ANTHROPIC_API_KEY": K1}),
        lambda r: httpx2.Response(200, json=_message(reply)),
    )
    out = await client.chat_json("judge", _MSG, _Tiny)
    await client.aclose()
    assert out.ok
    body = json.loads(seen[0].content)
    assert "Reply with JSON only" in body["system"]
    assert body["max_tokens"] > 0  # chat_json passes None; Messages API requires a value
    assert "tool_choice" not in body and "response_format" not in body


async def test_token_accounting_in_audit_without_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    audit = AuditLog(tmp_path / "audit.db")
    client, metas, _, _ = _client(
        tmp_path,
        _settings({"ANTHROPIC_API_KEY": K1}),
        lambda r: httpx2.Response(200, json=_message()),
    )
    client.on_call = audit_llm_calls(audit, then=metas.append)
    with caplog.at_level(logging.DEBUG), llm_call_scope("call-1"):
        await client.chat_text("judge", _MSG, max_tokens=400)
    await client.aclose()
    rows = audit.for_call("call-1")
    assert rows[0]["type"] == "llm_call"
    payload = rows[0]["payload"]
    assert payload["provider"] == "anthropic" and payload["model"] == "claude-sonnet-5-5"
    assert payload["prompt_tokens"] == 123 and payload["completion_tokens"] == 45
    assert K1 not in json.dumps(rows, default=str) + caplog.text + json.dumps(metas)
    audit.close()


async def test_429_cools_key_and_moves_to_next(tmp_path: Path) -> None:
    pool = {"ANTHROPIC_API_KEY_1": K1, "ANTHROPIC_API_KEY_2": K2}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers["x-api-key"] == K1:
            return _error(429, "rate_limit_error", retry_after="30")
        return httpx2.Response(200, json=_message())

    client, metas, seen, free = _client(tmp_path, _settings(pool), handler)
    await client.chat_text("judge", _MSG, max_tokens=400)
    await client.aclose()
    assert [r.headers["x-api-key"] for r in seen] == [K1, K2]
    assert metas[0]["key_id"] == "anthropic#1" and "429" in metas[0]["error"]
    assert metas[1]["key_id"] == "anthropic#2" and metas[1]["error"] is None
    assert 25.0 < client._cooling_s(client._keys["anthropic"][0], "claude-sonnet-5-5") <= 30.0
    assert free == []


async def test_overloaded_529_retries_then_fails_loudly_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.llm.client.random.random", lambda: 0.0)  # backoff 0.5 s, 1 s
    client, metas, seen, free = _client(
        tmp_path,
        _settings({"ANTHROPIC_API_KEY": K1}, llm_timeout_judge_s=0.8),
        lambda r: _error(529, "overloaded_error"),
    )
    with pytest.raises(LLMUnavailable) as exc:
        await client.chat_text("judge", _MSG, max_tokens=400)
    await client.aclose()
    assert len(seen) == 2  # first try + one backoff retry before the deadline
    assert free == []  # the judge never falls back to the free tier
    assert metas[-1]["error"] and K1 not in str(exc.value)


async def test_5xx_after_retries_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.client.asyncio.sleep", _no_sleep)
    client, metas, seen, free = _client(
        tmp_path, _settings({"ANTHROPIC_API_KEY": K1}), lambda r: _error(500, "api_error")
    )
    with pytest.raises(LLMUnavailable, match="5xx exhausted"):
        await client.chat_text("judge", _MSG, max_tokens=400)
    await client.aclose()
    assert len(seen) == 4 and free == []


async def _no_sleep(_s: float) -> None:
    return None


async def test_refusal_and_truncation_fail_instead_of_returning_empty(tmp_path: Path) -> None:
    for stop in ("refusal", "max_tokens"):
        client, _, _, _ = _client(
            tmp_path,
            _settings({"ANTHROPIC_API_KEY": K1}),
            lambda r, s=stop: httpx2.Response(200, json=_message("", stop=s)),
        )
        with pytest.raises(LLMUnavailable, match=stop):
            await client.chat_text("judge", _MSG, max_tokens=400)
        await client.aclose()


async def test_401_disables_key_and_error_is_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def echo(request: httpx2.Request) -> httpx2.Response:
        key = request.headers["x-api-key"]
        body = {"type": "error", "error": {"type": "authentication_error", "message": key}}
        return httpx2.Response(401, json=body)

    client, metas, _, _ = _client(tmp_path, _settings({"ANTHROPIC_API_KEY": K1}), echo)
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMUnavailable) as exc:
        await client.chat_text("judge", _MSG, max_tokens=400)
    await client.aclose()
    blob = json.dumps(metas) + caplog.text + str(exc.value) + repr(exc.value)
    assert K1 not in blob and "anthropic#0" in blob


async def test_judge_without_route_does_not_borrow_nlu(tmp_path: Path) -> None:
    client, _, seen, free = _client(
        tmp_path, _settings({"ANTHROPIC_API_KEY": K1}, llm_profile="demo"), lambda r: None
    )
    with pytest.raises(LLMUnavailable, match="judge"):
        await client.chat_text("judge", _MSG, max_tokens=400)
    await client.aclose()
    assert seen == [] and free == []


def test_judge_skipped_without_key(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path, _settings({}), lambda r: None)
    assert "anthropic" not in client._keys


def test_forbidden_anthropic_params_rejected(tmp_path: Path) -> None:
    for params in (
        "{thinking: {type: disabled}}",
        "{tool_choice: {type: any}}",
        "{temperature: 0}",
    ):
        path = tmp_path / "bad.yaml"
        path.write_text(_YAML.replace("{output_config: {effort: low}}", params))
        with pytest.raises(ValueError, match="anthropic"):
            LLMClient(_settings({}), providers_path=path, skip_health_check=True)


def test_shipped_judge_route_is_sonnet_5_5_without_fallback() -> None:
    import yaml

    data = yaml.safe_load(Path("config/providers.yaml").read_text())
    assert data["providers"]["anthropic"]["api"] == "anthropic"
    assert data["providers"]["anthropic"]["key_env"] == "ANTHROPIC_API_KEY"
    for name in ("eval", "local"):
        route = [parse_route_entry(e) for e in data["profiles"][name]["judge"]]
        assert [t.spec for t in route] == ["anthropic/claude-sonnet-5-5"], name
    # No other role moved off the free tier, except the demo NLU (Phase 41),
    # whose Claude target is budgeted (tests/unit/test_llm_budget.py).
    for pname, prof in data["profiles"].items():
        for role, entries in prof.items():
            if role == "judge":
                continue
            for t in map(parse_route_entry, entries):
                if t.provider == "anthropic":
                    assert (pname, role, t.budgeted) == ("demo", "nlu", True)


def test_judge_timeout_setting() -> None:
    assert Settings(_env_file=None).llm_timeout_judge_s > 0
