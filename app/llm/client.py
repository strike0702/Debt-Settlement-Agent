"""Role-based OpenAI-compatible LLM client with provider failover.

Callers ask for a role (``nlu`` / ``nlg`` / ``sim`` / ``stt``), never a model
name. Routing comes from ``config/providers.yaml`` profiles. Per-provider token
bucket + optional ``min_interval_s``; short 429 waits retry the same target,
long waits / daily quota mark it exhausted and fail over. Gemini free-tier
quota often arrives as HTTP 400 — remapped to 429 when ``quota_as_400``.

Does not own NLU/NLG prompts or policy. SQLite response cache (temp 0 only).
``FakeLLM`` is the offline/test stand-in with a per-role response queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
import yaml
from openai import APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel

from app.config import Settings, get_settings

Role = Literal["nlu", "nlg", "sim", "stt"]
T = TypeVar("T", bound=BaseModel)

OnCallHook = Callable[[dict[str, Any]], Awaitable[None] | None]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PROVIDERS = _REPO_ROOT / "config" / "providers.yaml"
_WHISPER_PROMPT = "settlement, minimum payment, balloon, monthly payments, percent"
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)
_SHORT_RETRY_S = 20.0

_KEY_ATTR = {
    "GROQ_API_KEY": "groq_api_key",
    "MISTRAL_API_KEY": "mistral_api_key",
    "GEMINI_API_KEY": "gemini_api_key",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "CEREBRAS_API_KEY": "cerebras_api_key",
}


class LLMUnavailable(Exception):
    """Every routed target exhausted or otherwise unusable."""


def strip_json_fences(text: str) -> str:
    """Remove optional markdown code fences around a JSON payload."""
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return s


def _parse_target(spec: str) -> tuple[str, str]:
    """Split ``provider/model`` on the first slash (model may contain slashes)."""
    if "/" not in spec:
        return spec, ""
    provider, model = spec.split("/", 1)
    return provider, model


def _cache_key(
    provider: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: float,
    max_tokens: int | None,
) -> str:
    payload = {
        "provider": provider,
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class _ProviderCfg:
    name: str
    base_url: str
    key_env: str | None = None
    api_key: str | None = None
    rpm: float = 0
    min_interval_s: float = 0.0
    json_mode: bool = True
    quota_as_400: bool = False
    local: bool = False
    health: str | None = None


class _TokenBucket:
    """Async token bucket; ``rpm == 0`` disables limiting."""

    def __init__(self, rpm: float, min_interval_s: float = 0.0) -> None:
        self.rpm = rpm
        self.min_interval_s = min_interval_s
        self._tokens = 1.0 if rpm > 0 else float("inf")
        self._capacity = max(1.0, rpm / 60.0) if rpm > 0 else float("inf")
        self._refill_per_s = rpm / 60.0 if rpm > 0 else 0.0
        self._last = time.monotonic()
        self._last_acquire = 0.0
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        if self.rpm <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_s)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._refill()
                wait_interval = 0.0
                if self.min_interval_s > 0 and self._last_acquire > 0:
                    since = time.monotonic() - self._last_acquire
                    if since < self.min_interval_s:
                        wait_interval = self.min_interval_s - since
                if self.rpm <= 0:
                    if wait_interval > 0:
                        await asyncio.sleep(wait_interval)
                    self._last_acquire = time.monotonic()
                    return
                if self._tokens >= 1.0 and wait_interval <= 0:
                    self._tokens -= 1.0
                    self._last_acquire = time.monotonic()
                    return
                wait_token = 0.0
                if self._tokens < 1.0 and self._refill_per_s > 0:
                    wait_token = (1.0 - self._tokens) / self._refill_per_s
                await asyncio.sleep(max(wait_interval, wait_token, 0.001))


class _ResponseCache:
    """SQLite cache for temperature-0 chat responses."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (
                key TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, key: str) -> tuple[str, int | None, int | None] | None:
        row = self._conn.execute(
            "SELECT body, prompt_tokens, completion_tokens FROM llm_cache WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return row[0], row[1], row[2]

    def put(
        self,
        key: str,
        body: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO llm_cache
                (key, body, prompt_tokens, completion_tokens, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                key,
                body,
                prompt_tokens,
                completion_tokens,
                datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


@dataclass
class _TargetState:
    exhausted_until: float | None = None  # monotonic deadline


class FakeLLM:
    """Scripted LLM for offline tests and the ``offline`` profile."""

    def __init__(self) -> None:
        self._queues: dict[Role, deque[Any]] = defaultdict(deque)
        self.calls: list[dict[str, Any]] = []
        self.on_call: OnCallHook | None = None

    def enqueue(self, role: Role, response: Any) -> None:
        """Queue a JSON-serializable / string / BaseModel response for ``role``."""
        self._queues[role].append(response)

    def _pop(self, role: Role) -> Any:
        q = self._queues[role]
        if not q:
            raise LLMUnavailable(f"FakeLLM queue empty for role={role}")
        return q.popleft()

    async def _emit(self, meta: dict[str, Any]) -> None:
        self.calls.append(meta)
        if self.on_call is not None:
            result = self.on_call(meta)
            if asyncio.iscoroutine(result):
                await result

    async def chat_json(
        self,
        role: Role,
        messages: Sequence[Mapping[str, Any]],
        schema: type[T],
    ) -> T:
        raw = self._pop(role)
        await self._emit(
            {
                "role": role,
                "provider": "fake",
                "model": "fake",
                "latency_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit": False,
                "failover_from": None,
            }
        )
        if isinstance(raw, schema):
            return raw
        if isinstance(raw, BaseModel):
            return schema.model_validate(raw.model_dump())
        if isinstance(raw, str):
            return schema.model_validate_json(strip_json_fences(raw))
        return schema.model_validate(raw)

    async def chat_text(
        self,
        role: Role,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> str:
        raw = self._pop(role)
        await self._emit(
            {
                "role": role,
                "provider": "fake",
                "model": "fake",
                "latency_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit": False,
                "failover_from": None,
            }
        )
        if isinstance(raw, str):
            return raw
        if isinstance(raw, BaseModel):
            return raw.model_dump_json()
        return json.dumps(raw)

    async def transcribe(self, wav_bytes: bytes, prompt: str | None = None) -> str:
        raw = self._pop("stt")
        await self._emit(
            {
                "role": "stt",
                "provider": "fake",
                "model": "fake",
                "latency_ms": 0,
                "prompt_tokens": None,
                "completion_tokens": None,
                "cache_hit": False,
                "failover_from": None,
            }
        )
        return str(raw)


class LLMClient:
    """Role-routed chat / transcribe client over the provider pool."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        providers_path: str | Path | None = None,
        http_clients: Mapping[str, httpx.AsyncClient] | None = None,
        on_call: OnCallHook | None = None,
        fake: FakeLLM | None = None,
        skip_health_check: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.on_call = on_call
        self._fake = fake
        self._http_clients = dict(http_clients or {})
        self._owned_http: list[httpx.AsyncClient] = []
        self._providers: dict[str, _ProviderCfg] = {}
        self._profiles: dict[str, dict[str, list[str]]] = {}
        self._openai: dict[str, AsyncOpenAI] = {}
        self._limiters: dict[str, _TokenBucket] = {}
        self._target_state: dict[tuple[str, str], _TargetState] = defaultdict(_TargetState)
        self._ollama_tags: set[str] | None = None
        self._ollama_ok = False
        self._cache: _ResponseCache | None = None
        path = Path(providers_path) if providers_path else _DEFAULT_PROVIDERS
        self._load_yaml(path)
        if self.settings.llm_cache:
            self._cache = _ResponseCache(self.settings.llm_cache_path)
        if self._fake is None and self.settings.llm_profile == "offline":
            self._fake = FakeLLM()
            self._fake.on_call = on_call
        if self._fake is None:
            self._init_providers(skip_health_check=skip_health_check)

    def _load_yaml(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text())
        for name, raw in (data.get("providers") or {}).items():
            self._providers[name] = _ProviderCfg(
                name=name,
                base_url=raw["base_url"],
                key_env=raw.get("key_env"),
                api_key=raw.get("api_key"),
                rpm=float(raw.get("rpm") or 0),
                min_interval_s=float(raw.get("min_interval_s") or 0),
                json_mode=bool(raw.get("json_mode", True)),
                quota_as_400=bool(raw.get("quota_as_400", False)),
                local=bool(raw.get("local", False)),
                health=raw.get("health"),
            )
        for pname, proute in (data.get("profiles") or {}).items():
            if "all" in proute:
                specs = list(proute["all"])
                self._profiles[pname] = {
                    "nlu": specs,
                    "nlg": specs,
                    "sim": specs,
                    "stt": specs,
                }
            else:
                self._profiles[pname] = {k: list(v) for k, v in proute.items()}

    def _resolve_key(self, cfg: _ProviderCfg) -> str | None:
        if cfg.api_key:
            return cfg.api_key
        if not cfg.key_env:
            return None
        attr = _KEY_ATTR.get(cfg.key_env)
        if not attr:
            return None
        val = getattr(self.settings, attr, None)
        if val is None or str(val).strip() == "":
            return None
        return str(val)

    def _init_providers(self, *, skip_health_check: bool) -> None:
        for name, cfg in self._providers.items():
            key = self._resolve_key(cfg)
            if key is None:
                continue
            if cfg.local and cfg.health and not skip_health_check:
                if not self._check_ollama(cfg):
                    continue
                self._ollama_ok = True
            http = self._http_clients.get(name)
            kwargs: dict[str, Any] = {
                "base_url": cfg.base_url,
                "api_key": key,
                "max_retries": 0,
            }
            if http is not None:
                kwargs["http_client"] = http
            self._openai[name] = AsyncOpenAI(**kwargs)
            self._limiters[name] = _TokenBucket(cfg.rpm, cfg.min_interval_s)

    def _check_ollama(self, cfg: _ProviderCfg) -> bool:
        base = cfg.base_url.rstrip("/")
        if base.endswith("/v1"):
            root = base[: -len("/v1")]
        else:
            root = base
        url = f"{root}{cfg.health}"
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    return False
                tags = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, OSError):
            return False
        models: set[str] = set()
        for m in tags.get("models") or []:
            n = m.get("name") or m.get("model") or ""
            if n:
                models.add(n)
                # Ollama tags often include :latest; also bare name
                if ":" in n:
                    models.add(n.split(":", 1)[0])
        self._ollama_tags = models
        return True

    def _temperature_for(self, role: Role) -> float:
        if role == "nlu" or role == "stt":
            return 0.0
        if self.settings.llm_profile == "eval":
            return 0.0
        return 0.4

    def _route(self, role: Role) -> list[str]:
        profile = self._profiles.get(self.settings.llm_profile)
        if not profile:
            raise LLMUnavailable(f"unknown llm_profile={self.settings.llm_profile!r}")
        specs = profile.get(role) or profile.get("nlu") or []
        return list(specs)

    def _provider_usable(self, provider: str, model: str) -> bool:
        if provider == "fake":
            return self._fake is not None
        if provider not in self._openai:
            return False
        cfg = self._providers[provider]
        if cfg.local and self._ollama_tags is not None:
            if model not in self._ollama_tags and model.split(":")[0] not in self._ollama_tags:
                return False
        state = self._target_state[(provider, model)]
        if state.exhausted_until is not None and time.monotonic() < state.exhausted_until:
            return False
        return True

    async def _emit(self, meta: dict[str, Any]) -> None:
        if self.on_call is not None:
            result = self.on_call(meta)
            if asyncio.iscoroutine(result):
                await result

    def _mark_exhausted(self, provider: str, model: str, retry_after_s: float | None) -> None:
        if retry_after_s is not None and retry_after_s > 0:
            until = time.monotonic() + retry_after_s
        else:
            # next UTC midnight as a conservative daily-quota reset
            now = datetime.now(UTC)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            until = time.monotonic() + max(60.0, (tomorrow - now).total_seconds())
        self._target_state[(provider, model)].exhausted_until = until

    def _retry_after_seconds(self, err: APIStatusError) -> float | None:
        headers = getattr(err, "headers", None)
        if headers is None and getattr(err, "response", None) is not None:
            headers = err.response.headers
        raw = None
        if headers is not None and hasattr(headers, "get"):
            raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _is_gemini_quota_400(self, provider: str, err: APIStatusError) -> bool:
        cfg = self._providers.get(provider)
        if not cfg or not cfg.quota_as_400:
            return False
        if err.status_code != 400:
            return False
        body = ""
        try:
            body = str(err.body) if err.body is not None else str(err)
        except Exception:
            body = str(err)
        low = body.lower()
        return "resource_exhausted" in low or "quota" in low

    async def chat_json(
        self,
        role: Role,
        messages: Sequence[Mapping[str, Any]],
        schema: type[T],
    ) -> T:
        if self._fake is not None and self.settings.llm_profile == "offline":
            return await self._fake.chat_json(role, messages, schema)
        text = await self._chat(role, list(messages), schema=schema, max_tokens=None)
        return schema.model_validate_json(strip_json_fences(text))

    async def chat_text(
        self,
        role: Role,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> str:
        if self._fake is not None and self.settings.llm_profile == "offline":
            return await self._fake.chat_text(role, messages, max_tokens)
        return await self._chat(role, list(messages), schema=None, max_tokens=max_tokens)

    async def _chat(
        self,
        role: Role,
        messages: list[Mapping[str, Any]],
        *,
        schema: type[BaseModel] | None,
        max_tokens: int | None,
    ) -> str:
        want_json = schema is not None
        temperature = self._temperature_for(role)
        route = self._route(role)
        failover_from: str | None = None
        last_err: Exception | None = None

        for spec in route:
            provider, model = _parse_target(spec)
            if provider == "fake":
                if self._fake is None:
                    continue
                if want_json and schema is not None:
                    obj = await self._fake.chat_json(role, messages, schema)
                    return obj.model_dump_json()
                return await self._fake.chat_text(role, messages, max_tokens or 256)

            if not self._provider_usable(provider, model):
                continue

            cfg = self._providers[provider]

            if (
                self._cache is not None
                and temperature == 0.0
                and self.settings.llm_cache
            ):
                key = _cache_key(provider, model, messages, temperature, max_tokens)
                hit = self._cache.get(key)
                if hit is not None:
                    body, pt, ct = hit
                    await self._emit(
                        {
                            "role": role,
                            "provider": provider,
                            "model": model,
                            "latency_ms": 0,
                            "prompt_tokens": pt,
                            "completion_tokens": ct,
                            "cache_hit": True,
                            "failover_from": failover_from,
                        }
                    )
                    return body

            msgs = list(messages)
            if want_json and not cfg.json_mode:
                msgs = [
                    *msgs,
                    {"role": "system", "content": "Reply with JSON only. No markdown fences."},
                ]

            try:
                body, pt, ct, latency_ms = await self._call_chat(
                    provider,
                    model,
                    msgs,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=want_json and cfg.json_mode,
                )
            except _TargetExhausted as e:
                last_err = e
                failover_from = f"{provider}/{model}"
                continue
            except _TargetFailed as e:
                last_err = e
                failover_from = f"{provider}/{model}"
                continue
            except Exception as e:
                last_err = e
                failover_from = f"{provider}/{model}"
                continue

            if self._cache is not None and temperature == 0.0 and self.settings.llm_cache:
                self._cache.put(
                    _cache_key(provider, model, messages, temperature, max_tokens),
                    body,
                    pt,
                    ct,
                )

            await self._emit(
                {
                    "role": role,
                    "provider": provider,
                    "model": model,
                    "latency_ms": latency_ms,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "cache_hit": False,
                    "failover_from": failover_from,
                }
            )
            return body

        raise LLMUnavailable(str(last_err) if last_err else "no usable LLM targets")

    async def _call_chat(
        self,
        provider: str,
        model: str,
        messages: list[Mapping[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
    ) -> tuple[str, int | None, int | None, float]:
        client = self._openai[provider]
        limiter = self._limiters[provider]
        backoff_s = [1.0, 2.0, 4.0]
        attempt_5xx = 0

        while True:
            await limiter.acquire()
            t0 = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                }
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(**kwargs)
            except RateLimitError as e:
                ra = self._retry_after_seconds(e)
                if ra is not None and ra <= _SHORT_RETRY_S:
                    await asyncio.sleep(ra)
                    continue
                self._mark_exhausted(provider, model, ra)
                raise _TargetExhausted(f"{provider}/{model} rate limited: {e}") from e
            except APIStatusError as e:
                if self._is_gemini_quota_400(provider, e):
                    ra = self._retry_after_seconds(e)
                    if ra is not None and ra <= _SHORT_RETRY_S:
                        await asyncio.sleep(ra)
                        continue
                    self._mark_exhausted(provider, model, ra)
                    raise _TargetExhausted(
                        f"{provider}/{model} gemini quota (400→429): {e}"
                    ) from e
                if e.status_code == 429:
                    ra = self._retry_after_seconds(e)
                    if ra is not None and ra <= _SHORT_RETRY_S:
                        await asyncio.sleep(ra)
                        continue
                    self._mark_exhausted(provider, model, ra)
                    raise _TargetExhausted(f"{provider}/{model} 429: {e}") from e
                if 500 <= e.status_code < 600:
                    if attempt_5xx < len(backoff_s):
                        delay = backoff_s[attempt_5xx] * (0.5 + random.random())
                        attempt_5xx += 1
                        await asyncio.sleep(delay)
                        continue
                    raise _TargetFailed(f"{provider}/{model} 5xx exhausted retries: {e}") from e
                raise _TargetFailed(f"{provider}/{model} HTTP {e.status_code}: {e}") from e

            latency_ms = (time.perf_counter() - t0) * 1000.0
            choice = resp.choices[0].message.content or ""
            usage = resp.usage
            pt = usage.prompt_tokens if usage else None
            ct = usage.completion_tokens if usage else None
            return choice, pt, ct, latency_ms

    async def transcribe(self, wav_bytes: bytes, prompt: str | None = None) -> str:
        if self._fake is not None and self.settings.llm_profile == "offline":
            return await self._fake.transcribe(wav_bytes, prompt)

        route = self._route("stt")
        failover_from: str | None = None
        last_err: Exception | None = None
        use_prompt = prompt if prompt is not None else _WHISPER_PROMPT

        for spec in route:
            provider, model = _parse_target(spec)
            if not self._provider_usable(provider, model):
                continue
            client = self._openai[provider]
            limiter = self._limiters[provider]
            try:
                await limiter.acquire()
                t0 = time.perf_counter()
                # openai SDK expects a file-like tuple for upload
                import io

                bio = io.BytesIO(wav_bytes)
                bio.name = "audio.wav"
                resp = await client.audio.transcriptions.create(
                    model=model,
                    file=bio,
                    language="en",
                    temperature=0,
                    prompt=use_prompt,
                )
                latency_ms = (time.perf_counter() - t0) * 1000.0
                text = resp.text if hasattr(resp, "text") else str(resp)
                await self._emit(
                    {
                        "role": "stt",
                        "provider": provider,
                        "model": model,
                        "latency_ms": latency_ms,
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "cache_hit": False,
                        "failover_from": failover_from,
                    }
                )
                return text
            except RateLimitError as e:
                ra = self._retry_after_seconds(e)
                if ra is not None and ra <= _SHORT_RETRY_S:
                    await asyncio.sleep(ra)
                    # retry same target once by looping without advancing
                    try:
                        await limiter.acquire()
                        t0 = time.perf_counter()
                        import io

                        bio = io.BytesIO(wav_bytes)
                        bio.name = "audio.wav"
                        resp = await client.audio.transcriptions.create(
                            model=model,
                            file=bio,
                            language="en",
                            temperature=0,
                            prompt=use_prompt,
                        )
                        latency_ms = (time.perf_counter() - t0) * 1000.0
                        text = resp.text if hasattr(resp, "text") else str(resp)
                        await self._emit(
                            {
                                "role": "stt",
                                "provider": provider,
                                "model": model,
                                "latency_ms": latency_ms,
                                "prompt_tokens": None,
                                "completion_tokens": None,
                                "cache_hit": False,
                                "failover_from": failover_from,
                            }
                        )
                        return text
                    except Exception as e2:
                        last_err = e2
                        self._mark_exhausted(provider, model, ra)
                        failover_from = f"{provider}/{model}"
                        continue
                self._mark_exhausted(provider, model, ra)
                last_err = e
                failover_from = f"{provider}/{model}"
                continue
            except Exception as e:
                last_err = e
                failover_from = f"{provider}/{model}"
                continue

        raise LLMUnavailable(str(last_err) if last_err else "no usable STT targets")

    async def aclose(self) -> None:
        for c in self._openai.values():
            await c.close()
        for h in self._owned_http:
            await h.aclose()
        if self._cache is not None:
            self._cache.close()


class _TargetExhausted(Exception):
    """Target marked exhausted; caller should fail over."""


class _TargetFailed(Exception):
    """Hard failure on target after retries; caller should fail over."""


def make_client(
    settings: Settings | None = None,
    **kwargs: Any,
) -> LLMClient | FakeLLM:
    """Build an ``LLMClient``, or a bare ``FakeLLM`` when profile is offline."""
    s = settings or get_settings()
    if s.llm_profile == "offline" and "fake" not in kwargs:
        fake = FakeLLM()
        fake.on_call = kwargs.get("on_call")
        return fake
    return LLMClient(settings=s, **kwargs)
