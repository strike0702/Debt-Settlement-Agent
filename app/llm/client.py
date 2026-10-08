"""Role-based LLM client (OpenAI-compatible + Anthropic) with provider failover.

Callers ask for a role (``nlu`` / ``nlg`` / ``sim`` / ``stt`` / ``agent`` /
``judge``), never a model name. ``agent`` is the eval-only A/B arms' move role
(``eval/agents``); a profile without an ``agent`` route falls back to its
``nlu`` route. ``judge`` (Phase 30, ``eval/judge_naturalness``) never falls
back: a profile without a ``judge`` route raises ``LLMUnavailable``, because a
judge that silently changes model would corrupt the metric.

Providers speak the OpenAI chat API unless their config sets ``api:
anthropic``; those go through the official ``anthropic`` SDK (Messages API):
system messages fold into ``system``, JSON mode is a prompt line plus the
caller's local parse (no forced ``tool_choice``), no ``temperature`` or
``thinking`` is sent (Sonnet 5.5 rejects non-default sampling and disabled
thinking), route ``params`` (e.g. ``output_config: {effort: low}``) go in the
body, and a ``refusal`` or an empty ``max_tokens`` reply fails the target.
Anthropic 429 / 529 / 5xx / 401 use the same pool handling as below.
Routing comes from ``config/providers.yaml`` profiles; a route entry is
either ``provider/model`` or ``{target: provider/model, params: {...},
timeout_s: N}`` (params go into the request body and the response-cache key;
``timeout_s`` overrides the role timeout for that target only).

Key pools: a provider's keys come from ``Settings.api_keys(key_env)``
(``NAME`` plus ``NAME_1``, ``NAME_2``, ...). Successive requests round-robin over
the pool. Buckets are keyed ``(provider, key suffix, model)``: an rpm bucket
(burst ``min(rpm, 5)``, refill ``rpm/60``/s, optional ``min_interval_s``) and,
when the provider sets ``tpm``, a tokens-per-minute budget (charged with a
prompt estimate, reconciled from ``usage``). A 429 / quota error cools that key
for that model (Retry-After, a delay parsed from the error body — Groq "try
again in", Gemini ``retryDelay`` — at least ``DAILY_QUOTA_COOLDOWN_S`` for a
per-day quota without a provider delay, else ``Settings.llm_key_cooldown_s``) and the same
target is retried at once on the next key; only when every key is cooling does
the target wait out a short cooldown or fail over. 401/403 disables the key for
the process (logged once). Gemini free-tier quota often arrives as HTTP 400 —
treated as 429 when ``quota_as_400``. One deadline per target (route
``timeout_s`` or ``Settings.llm_timeout_<role>_s``) covers limiter waits,
requests, 5xx backoff, cooldown waits and key switches; a timeout, connection
error, or HTTP error fails over to the next target. Any other exception is a
bug and propagates. Key values never appear in logs, metas, errors or cache
keys; they are named by label (``groq#2`` = ``GROQ_API_KEY_2``, ``#0`` = unsuffixed).

``on_call`` receives one meta dict per attempt that finished: successes and
cache hits (``error=None``), failed targets (``error=<message>``), and a key
that failed before the call moved to another key. Each meta carries ``key_id``
(key label, ``None`` for cache hits / fake) and ``queue_ms`` (limiter wait plus
cooldown waits); ``queue_wait_scope`` sums it for a turn.
``app.llm.call_audit`` turns that hook into audit-log rows.

Daily budget (Phase 41): a route entry may carry ``budgeted: true`` (only the
demo ``nlu`` route's Claude target does; a ``judge`` route may not, so eval
judging is never capped). Its provider must list the model under
``prices_usd_per_mtok``. Before such a target, once its provider has a usable
key, ``app.llm.budget.DailyBudget`` is checked: when today's (UTC) spend has
reached ``Settings.claude_daily_budget_usd`` the target is skipped and the call
goes down the route (``failover_from`` = the skipped target). The skip emits a
meta with ``event="llm_budget_skip"`` (and, first time each day,
``event="llm_budget_exhausted"``), ``error`` set and ``latency_ms`` 0. A live
success adds ``prompt_tokens`` × input price + ``completion_tokens`` × output
price to the day; failures and cache hits add nothing.

Does not own NLU/NLG prompts or policy. SQLite response cache (temp 0 only).
``FakeLLM`` is the offline/test stand-in with a per-role response queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import random
import re
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

import anthropic
import httpx
import yaml
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.llm.budget import DailyBudget, ModelPrice, micros_to_usd, usd_to_micros

Role = Literal["nlu", "nlg", "sim", "stt", "agent", "judge"]

# Both SDKs' status / connection errors take the same pool path (cooldown, retry, failover).
_STATUS_ERRORS = (APIStatusError, anthropic.APIStatusError)
_CONNECTION_ERRORS = (APIConnectionError, anthropic.APIConnectionError)
_AnyStatusError = APIStatusError | anthropic.APIStatusError
# Roles that must not borrow the profile's nlu route when they have none.
_NO_NLU_FALLBACK: frozenset[str] = frozenset({"judge"})
# The Messages API requires max_tokens; chat_json passes None.
_ANTHROPIC_DEFAULT_MAX_TOKENS = 1024
# Route params an anthropic target may not carry: forced tool use, sampling, and
# disabled thinking all 400 on Sonnet 5.5 (and JSON is emulated, never forced).
_ANTHROPIC_BANNED_PARAMS = frozenset({"tool_choice", "temperature", "top_p", "top_k"})

# A per-day quota does not reset in a minute: cool the key long enough that the
# call moves to other keys / targets instead of re-hitting it every minute.
DAILY_QUOTA_COOLDOWN_S = 3600.0
_PER_DAY_RE = re.compile(r"per[ _-]?day|perday|\((?:TPD|RPD)\)", re.IGNORECASE)
_TRY_AGAIN_RE = re.compile(
    r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", re.IGNORECASE
)
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?([\d.]+)s", re.IGNORECASE)
T = TypeVar("T", bound=BaseModel)
_log = logging.getLogger(__name__)

OnCallHook = Callable[[dict[str, Any]], Awaitable[None] | None]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PROVIDERS = _REPO_ROOT / "config" / "providers.yaml"
_WHISPER_PROMPT = "settlement, minimum payment, balloon, monthly payments, percent"
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)
_SHORT_RETRY_S = 20.0
_MAX_SHORT_RETRIES = 3
# Burst capacity cap: a turn makes at most ~3 calls (STT, NLU, NLG) back to back.
_BURST_CAP = 5.0



def _estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Rough prompt size for the TPM budget (~4 chars/token plus per-message overhead)."""
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return chars // 4 + 4 * len(messages)


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


class QueueWait:
    """Mutable sum of limiter wait (ms) for the calls made inside one scope."""

    def __init__(self) -> None:
        self.ms = 0.0


_QUEUE_WAIT: ContextVar[QueueWait | None] = ContextVar("llm_queue_wait", default=None)


@contextmanager
def queue_wait_scope() -> Iterator[QueueWait]:
    """Sum ``queue_ms`` of every LLM / STT attempt made inside this block."""
    acc = QueueWait()
    token = _QUEUE_WAIT.set(acc)
    try:
        yield acc
    finally:
        _QUEUE_WAIT.reset(token)


def _parse_target(spec: str) -> tuple[str, str]:
    """Split ``provider/model`` on the first slash (model may contain slashes)."""
    if "/" not in spec:
        return spec, ""
    provider, model = spec.split("/", 1)
    return provider, model


@dataclass(frozen=True)
class RouteTarget:
    """One routed target: ``provider/model``, optional request ``params`` and ``timeout_s``."""

    provider: str
    model: str
    params: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: float | None = None
    # Phase 41: subject to the daily budget (see module docstring).
    budgeted: bool = False

    @property
    def spec(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider


def parse_route_entry(raw: str | Mapping[str, Any]) -> RouteTarget:
    """Parse a providers.yaml route entry (plain string or ``{target, params}``)."""
    if isinstance(raw, str):
        provider, model = _parse_target(raw)
        return RouteTarget(provider, model)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("target"), str):
        raise ValueError(f"route entry needs a 'target' string: {raw!r}")
    extra = set(raw) - {"target", "params", "timeout_s", "budgeted"}
    if extra:
        raise ValueError(f"unknown route entry keys {sorted(extra)}: {raw!r}")
    params = raw.get("params") or {}
    if not isinstance(params, Mapping):
        raise ValueError(f"route params must be a mapping: {raw!r}")
    timeout_s = raw.get("timeout_s")
    if timeout_s is not None and (
        isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0
    ):
        raise ValueError(f"route timeout_s must be a positive number: {raw!r}")
    budgeted = raw.get("budgeted", False)
    if not isinstance(budgeted, bool):
        raise ValueError(f"route budgeted must be true or false: {raw!r}")
    provider, model = _parse_target(raw["target"])
    return RouteTarget(
        provider,
        model,
        dict(params),
        float(timeout_s) if timeout_s is not None else None,
        budgeted,
    )


def _cache_key(
    provider: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: float,
    max_tokens: int | None,
    params: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Only add the key when set, so cache entries written before route params
    # existed (plain-string routes) still hit.
    if params:
        payload["params"] = dict(params)
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
    tpm: float = 0  # tokens per minute per key; 0 = no TPM budget
    api: Literal["openai", "anthropic"] = "openai"
    # model -> price, from ``prices_usd_per_mtok`` (Phase 41 daily budget).
    prices: dict[str, ModelPrice] = field(default_factory=dict)


@dataclass(eq=False)
class _ApiKey:
    """One key of a provider pool; ``secret`` never leaves this object."""

    provider: str
    suffix: int
    secret: str = field(repr=False)
    client: AsyncOpenAI | anthropic.AsyncAnthropic = field(repr=False)
    disabled: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider}#{self.suffix}"


class _TokenBucket:
    """Async token bucket; ``rpm == 0`` disables limiting.

    Starts full with burst capacity ``min(rpm, 5)`` and refills ``rpm/60`` tokens
    per second, so a turn's back-to-back calls do not wait while the sustained
    rate still respects ``rpm``.
    """

    def __init__(self, rpm: float, min_interval_s: float = 0.0) -> None:
        self.rpm = rpm
        self.min_interval_s = min_interval_s
        self._capacity = max(1.0, min(rpm, _BURST_CAP)) if rpm > 0 else float("inf")
        self._tokens = self._capacity
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

    def eta(self) -> float:
        """Seconds until ``acquire`` would return without waiting (0 when ready)."""
        self._refill()
        wait = 0.0
        if self.min_interval_s > 0 and self._last_acquire > 0:
            wait = max(0.0, self.min_interval_s - (time.monotonic() - self._last_acquire))
        if self.rpm > 0 and self._tokens < 1.0 and self._refill_per_s > 0:
            wait = max(wait, (1.0 - self._tokens) / self._refill_per_s)
        return wait

    async def acquire(self) -> None:
        """Wait for a token (and ``min_interval_s``); cancellation consumes none."""
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


class _TpmBudget:
    """Tokens-per-minute budget for one key and model (capacity ``tpm``, refill ``tpm/60``/s).

    ``acquire(cost)`` waits until ``cost`` tokens (capped at capacity) are free and
    charges them; ``reconcile`` charges the difference to actual ``usage``, so the
    balance may go negative after an under-estimate and the next caller waits.
    """

    def __init__(self, tpm: float) -> None:
        self._capacity = float(tpm)
        self._tokens = self._capacity
        self._refill_per_s = tpm / 60.0
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._refill_per_s)
        self._last = now

    def eta(self, cost: int) -> float:
        """Seconds until ``cost`` tokens are available (0 when ready)."""
        self._refill()
        need = min(float(cost), self._capacity)
        return max(0.0, (need - self._tokens) / self._refill_per_s)

    async def acquire(self, cost: int) -> None:
        while (wait := self.eta(cost)) > 0:
            await asyncio.sleep(max(wait, 0.001))
        self._tokens -= cost

    def reconcile(self, charged: int, actual: int) -> None:
        self._refill()
        self._tokens = min(self._capacity, self._tokens - (actual - charged))


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

    async def _take(self, role: Role) -> Any:
        """Pop the next scripted reply and emit its call meta (failure if empty)."""
        tokens = None if role == "stt" else 0
        meta: dict[str, Any] = {
            "role": role,
            "provider": "fake",
            "model": "fake",
            "latency_ms": 0,
            "prompt_tokens": tokens,
            "completion_tokens": tokens,
            "cache_hit": False,
            "failover_from": None,
            "error": None,
            "queue_ms": 0.0,
            "key_id": None,
        }
        try:
            raw = self._pop(role)
        except LLMUnavailable as e:
            await self._emit({**meta, "error": str(e)})
            raise
        await self._emit(meta)
        return raw

    async def chat_json(
        self,
        role: Role,
        messages: Sequence[Mapping[str, Any]],
        schema: type[T],
    ) -> T:
        raw = await self._take(role)
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
        *,
        json_mode: bool = False,
    ) -> str:
        raw = await self._take(role)
        if isinstance(raw, str):
            return raw
        if isinstance(raw, BaseModel):
            return raw.model_dump_json()
        return json.dumps(raw)

    async def transcribe(self, wav_bytes: bytes, prompt: str | None = None) -> str:
        raw = await self._take("stt")
        return str(raw)

    async def aclose(self) -> None:
        """No-op; matches ``LLMClient.aclose`` for shared teardown."""
        return None


class LLMClient:
    """Role-routed chat / transcribe client over the provider pool."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        providers_path: str | Path | None = None,
        http_clients: Mapping[str, Any] | None = None,
        on_call: OnCallHook | None = None,
        fake: FakeLLM | None = None,
        skip_health_check: bool = False,
        budget: DailyBudget | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # Built on first use (see _budget_for) so a keyless route never opens the DB.
        self._budget = budget
        self._fake = fake
        self._owns_fake = False
        self.on_call = on_call
        self._http_clients = dict(http_clients or {})
        self._providers: dict[str, _ProviderCfg] = {}
        self._profiles: dict[str, dict[str, list[RouteTarget]]] = {}
        # First key's client per provider (membership = provider has a usable key).
        self._openai: dict[str, AsyncOpenAI | anthropic.AsyncAnthropic] = {}
        self._keys: dict[str, list[_ApiKey]] = {}
        self._rr: dict[str, int] = defaultdict(int)
        # Keyed (provider, key suffix, model); see module docstring.
        self._limiters: dict[tuple[str, int, str], _TokenBucket] = {}
        self._tpm: dict[tuple[str, int, str], _TpmBudget] = {}
        self._cooldown: dict[tuple[str, int, str], float] = {}  # monotonic until
        self._ollama_tags: set[str] | None = None
        self._ollama_ok = False
        self._cache: _ResponseCache | None = None
        path = Path(providers_path) if providers_path else _DEFAULT_PROVIDERS
        self._load_yaml(path)
        if self.settings.llm_cache:
            self._cache = _ResponseCache(self.settings.llm_cache_path)
        if self._fake is None and self.settings.llm_profile == "offline":
            self._fake = FakeLLM()
            self._owns_fake = True
            self._fake.on_call = on_call
        if self._fake is None:
            self._init_providers(skip_health_check=skip_health_check)

    @property
    def on_call(self) -> OnCallHook | None:
        """Per-attempt meta hook (see module docstring)."""
        return self._on_call

    @on_call.setter
    def on_call(self, hook: OnCallHook | None) -> None:
        # The offline FakeLLM this client built emits on its own hook; keep them in sync
        # so audit wiring done after construction (main lifespan, eval) still applies.
        self._on_call = hook
        if self._owns_fake and self._fake is not None:
            self._fake.on_call = hook

    def _load_yaml(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text())
        for name, raw in (data.get("providers") or {}).items():
            api = raw.get("api", "openai")
            if api not in ("openai", "anthropic"):
                raise ValueError(f"provider {name}: unknown api {api!r}")
            prices = {
                str(m): ModelPrice.from_config(p, f"provider {name} price {m}")
                for m, p in (raw.get("prices_usd_per_mtok") or {}).items()
            }
            self._providers[name] = _ProviderCfg(
                name=name,
                base_url=raw["base_url"],
                key_env=raw.get("key_env"),
                api_key=raw.get("api_key"),
                rpm=float(raw.get("rpm") or 0),
                min_interval_s=float(raw.get("min_interval_s") or 0),
                # Anthropic has no JSON response_format: always the prompt line.
                json_mode=bool(raw.get("json_mode", True)) and api != "anthropic",
                api=api,
                quota_as_400=bool(raw.get("quota_as_400", False)),
                local=bool(raw.get("local", False)),
                health=raw.get("health"),
                tpm=float(raw.get("tpm") or 0),
                prices=prices,
            )
        for pname, proute in (data.get("profiles") or {}).items():
            if "all" in proute:
                specs = [parse_route_entry(e) for e in proute["all"]]
                self._profiles[pname] = {
                    "nlu": specs,
                    "nlg": specs,
                    "sim": specs,
                    "stt": specs,
                    "judge": specs,
                }
            else:
                self._profiles[pname] = {
                    k: [parse_route_entry(e) for e in v] for k, v in proute.items()
                }
        for pname, roles in self._profiles.items():
            for role, targets in roles.items():
                for t in targets:
                    cfg = self._providers.get(t.provider)
                    if cfg is not None and cfg.api == "anthropic":
                        _check_anthropic_params(f"{pname}/{t.spec}", t.params)
                    if not t.budgeted:
                        continue
                    # The eval judge has its own spend approval; a capped judge
                    # would also switch model mid-run, which _NO_NLU_FALLBACK forbids.
                    if role in _NO_NLU_FALLBACK:
                        raise ValueError(f"{pname}/{role}/{t.spec}: {role} may not be budgeted")
                    if cfg is None or t.model not in cfg.prices:
                        raise ValueError(
                            f"{pname}/{role}/{t.spec}: budgeted target needs a "
                            f"prices_usd_per_mtok entry on provider {t.provider!r}"
                        )

    def _resolve_keys(self, cfg: _ProviderCfg) -> list[tuple[int, str]]:
        """``(suffix, value)`` pool: inline ``api_key`` (local) or ``Settings.api_keys``."""
        if cfg.api_key:
            return [(0, cfg.api_key)]
        if not cfg.key_env:
            return []
        return self.settings.api_keys(cfg.key_env)

    def _init_providers(self, *, skip_health_check: bool) -> None:
        for name, cfg in self._providers.items():
            pool = self._resolve_keys(cfg)
            if not pool:
                continue
            if cfg.local and cfg.health and not skip_health_check:
                if not self._check_ollama(cfg):
                    continue
                self._ollama_ok = True
            http = self._http_clients.get(name)
            keys: list[_ApiKey] = []
            for suffix, secret in pool:
                # max_retries=0: the pool owns retries, cooldowns and failover.
                kwargs: dict[str, Any] = {
                    "base_url": cfg.base_url,
                    "api_key": secret,
                    "max_retries": 0,
                }
                if http is not None:
                    kwargs["http_client"] = http
                sdk = anthropic.AsyncAnthropic if cfg.api == "anthropic" else AsyncOpenAI
                keys.append(_ApiKey(name, suffix, secret, sdk(**kwargs)))
            self._keys[name] = keys
            self._openai[name] = keys[0].client

    def _redact(self, text: str) -> str:
        """Replace any key value in ``text`` with its label (provider error bodies may echo it)."""
        for keys in self._keys.values():
            for k in keys:
                if k.secret and k.secret in text:
                    text = text.replace(k.secret, k.label)
        return text

    def _limiter(self, provider: str, suffix: int, model: str) -> _TokenBucket:
        """rpm bucket for one ``(provider, key suffix, model)``; free-tier limits are per model."""
        bk = (provider, suffix, model)
        bucket = self._limiters.get(bk)
        if bucket is None:
            cfg = self._providers[provider]
            bucket = _TokenBucket(cfg.rpm, cfg.min_interval_s)
            self._limiters[bk] = bucket
        return bucket

    def _tpm_budget(self, provider: str, suffix: int, model: str) -> _TpmBudget | None:
        """TPM budget for one ``(provider, key suffix, model)``, or None when ``tpm`` is unset."""
        cfg = self._providers[provider]
        if cfg.tpm <= 0:
            return None
        bk = (provider, suffix, model)
        budget = self._tpm.get(bk)
        if budget is None:
            budget = _TpmBudget(cfg.tpm)
            self._tpm[bk] = budget
        return budget

    def _cooling_s(self, key: _ApiKey, model: str) -> float:
        until = self._cooldown.get((key.provider, key.suffix, model))
        return 0.0 if until is None else max(0.0, until - time.monotonic())

    def _cool(self, key: _ApiKey, model: str, retry_after_s: float | None) -> None:
        secs = retry_after_s if retry_after_s and retry_after_s > 0 else None
        if secs is None:
            secs = float(self.settings.llm_key_cooldown_s)
        self._cooldown[(key.provider, key.suffix, model)] = time.monotonic() + secs

    def _pick_key(self, provider: str, model: str, cost: int) -> _ApiKey | None:
        """Next live key in round-robin order, preferring one whose buckets are ready.

        Skips disabled and cooling keys. Among the rest, the first (from the
        cursor) with no limiter wait wins, else the one with the shortest wait.
        The cursor moves past the chosen key. None when no key is live.
        """
        keys = self._keys[provider]
        n = len(keys)
        start = self._rr[provider] % n
        best: tuple[float, int] | None = None
        for off in range(n):
            i = (start + off) % n
            k = keys[i]
            if k.disabled or self._cooling_s(k, model) > 0:
                continue
            eta = self._limiter(provider, k.suffix, model).eta()
            budget = self._tpm_budget(provider, k.suffix, model)
            if budget is not None and cost > 0:
                eta = max(eta, budget.eta(cost))
            if best is None or eta < best[0]:
                best = (eta, i)
            if eta <= 0:
                break
        if best is None:
            return None
        self._rr[provider] = best[1] + 1
        return keys[best[1]]

    def _soonest_cooldown_s(self, provider: str, model: str) -> float | None:
        """Shortest remaining cooldown among non-disabled keys; None if all are disabled."""
        waits = [self._cooling_s(k, model) for k in self._keys[provider] if not k.disabled]
        return min(waits) if waits else None

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

    def _timeout_for(self, role: Role) -> float:
        """Per-request timeout in seconds for ``role`` (from Settings)."""
        return float(getattr(self.settings, f"llm_timeout_{role}_s"))

    def _temperature_for(self, role: Role) -> float:
        if role == "nlu" or role == "stt":
            return 0.0
        if self.settings.llm_profile == "eval":
            return 0.0
        return 0.4

    def _route(self, role: Role) -> list[RouteTarget]:
        profile = self._profiles.get(self.settings.llm_profile)
        if not profile:
            raise LLMUnavailable(f"unknown llm_profile={self.settings.llm_profile!r}")
        specs = profile.get(role)
        if not specs and role in _NO_NLU_FALLBACK:
            raise LLMUnavailable(
                f"llm_profile={self.settings.llm_profile!r} has no {role} route"
            )
        return list(specs or profile.get("nlu") or [])

    def _provider_usable(self, provider: str, model: str, horizon_s: float) -> bool:
        """Has a live key, or one whose cooldown ends within ``horizon_s`` (short-wait cap)."""
        if provider == "fake":
            return self._fake is not None
        if provider not in self._keys:
            return False
        cfg = self._providers[provider]
        if cfg.local and self._ollama_tags is not None:
            if model not in self._ollama_tags and model.split(":")[0] not in self._ollama_tags:
                return False
        soonest = self._soonest_cooldown_s(provider, model)
        return soonest is not None and soonest <= min(horizon_s, _SHORT_RETRY_S)

    def _budget_for(self) -> DailyBudget:
        if self._budget is None:
            self._budget = DailyBudget(
                self.settings.db_path,
                limit_micros=usd_to_micros(self.settings.claude_daily_budget_usd),
            )
        return self._budget

    def budget_status(self) -> dict[str, Any]:
        """Today's budget as ``{day, spent_usd, limit_usd, remaining_usd}`` (Decimal USD)."""
        b = self._budget_for()
        spent = b.spent_micros()
        return {
            "day": b.today().isoformat(),
            "spent_usd": micros_to_usd(spent),
            "limit_usd": micros_to_usd(b.limit_micros),
            "remaining_usd": micros_to_usd(max(0, b.limit_micros - spent)),
        }

    async def _skip_over_budget(
        self, role: Role, target: RouteTarget, failover_from: str | None
    ) -> bool:
        """True (after emitting the budget markers) when ``target`` must be skipped today."""
        budget = self._budget_for()
        if not budget.exhausted():
            return False
        status = "daily budget exhausted"
        base: dict[str, Any] = {
            "role": role,
            "provider": target.provider,
            "model": target.model,
            "latency_ms": 0.0,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cache_hit": False,
            "failover_from": failover_from,
            "error": status,
            "queue_ms": 0.0,
            "key_id": None,
        }
        if budget.note_exhausted():
            try:
                spent = budget.spent_micros()
            except sqlite3.Error:  # the marker must not fail the call
                spent = -1
            await self._emit(
                {
                    **base,
                    "event": "llm_budget_exhausted",
                    "day": budget.today().isoformat(),
                    "spent_usd": str(micros_to_usd(spent)) if spent >= 0 else None,
                    "limit_usd": str(micros_to_usd(budget.limit_micros)),
                }
            )
        await self._emit({**base, "event": "llm_budget_skip"})
        return True

    def _record_spend(
        self,
        target: RouteTarget,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int | None,
        pt: int | None,
        ct: int | None,
    ) -> None:
        """Charge one live budgeted success; missing usage is charged high, not as 0."""
        price = self._providers[target.provider].prices[target.model]
        pt_n = pt if pt is not None else _estimate_tokens(messages)
        ct_n = ct if ct is not None else (max_tokens or _ANTHROPIC_DEFAULT_MAX_TOKENS)
        self._budget_for().record(price, pt_n, ct_n)

    async def _emit(self, meta: dict[str, Any]) -> None:
        if self.on_call is not None:
            result = self.on_call(meta)
            if asyncio.iscoroutine(result):
                await result

    async def _emit_call(
        self,
        role: Role,
        provider: str,
        model: str,
        *,
        latency_ms: float,
        failover_from: str | None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cache_hit: bool = False,
        error: str | None = None,
        queue_ms: float = 0.0,
        key_id: str | None = None,
    ) -> None:
        acc = _QUEUE_WAIT.get()
        if acc is not None:
            acc.ms += queue_ms
        await self._emit(
            {
                "role": role,
                "provider": provider,
                "model": model,
                "latency_ms": latency_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit": cache_hit,
                "failover_from": failover_from,
                "error": error,
                "queue_ms": queue_ms,
                "key_id": key_id,
            }
        )

    def _retry_after_seconds(self, err: _AnyStatusError) -> float | None:
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

    @staticmethod
    def _error_body(err: _AnyStatusError) -> str:
        try:
            return str(err.body) if err.body is not None else str(err)
        except Exception:
            return str(err)

    def _quota_cooldown_s(self, err: _AnyStatusError) -> float | None:
        """Cooldown for a 429 / quota error: header, body delay, or a per-day floor.

        Groq's "try again in 7m12s" is trusted as is (rolling window). A per-day
        quota with no Groq delay (Gemini's ``retryDelay`` is often seconds even
        then) cools for at least ``DAILY_QUOTA_COOLDOWN_S``. None → default.
        """
        header = self._retry_after_seconds(err)
        body = self._error_body(err)
        groq: float | None = None
        m = _TRY_AGAIN_RE.search(body)
        if m and any(m.groups()):
            h, mins, secs = m.groups()
            groq = int(h or 0) * 3600 + int(mins or 0) * 60 + float(secs or 0)
        gemini: float | None = None
        m = _RETRY_DELAY_RE.search(body)
        if m:
            gemini = float(m.group(1))
        found = [x for x in (header, groq, gemini) if x is not None and x > 0]
        best = max(found) if found else None
        if _PER_DAY_RE.search(body) and groq is None:
            return max(best or 0.0, DAILY_QUOTA_COOLDOWN_S)
        return best

    def _is_gemini_quota_400(self, provider: str, err: _AnyStatusError) -> bool:
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

    @staticmethod
    async def _rate_wait(seconds: float, queue: QueueWait | None) -> None:
        """Sleep out a short 429 Retry-After; it is rate-limit wait, so it counts as queue."""
        t0 = time.perf_counter()
        await asyncio.sleep(seconds)
        if queue is not None:
            queue.ms += (time.perf_counter() - t0) * 1000.0

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
        *,
        json_mode: bool = False,
    ) -> str:
        """Raw reply text. ``json_mode`` asks for a JSON object (``response_format``
        on providers with ``json_mode``, a JSON-only system line elsewhere) but
        leaves parsing to the caller, which keeps its own lenient coerce."""
        if self._fake is not None and self.settings.llm_profile == "offline":
            return await self._fake.chat_text(role, messages, max_tokens)
        return await self._chat(
            role, list(messages), schema=None, max_tokens=max_tokens, json_text=json_mode
        )

    async def _chat(
        self,
        role: Role,
        messages: list[Mapping[str, Any]],
        *,
        schema: type[BaseModel] | None,
        max_tokens: int | None,
        json_text: bool = False,
    ) -> str:
        want_json = schema is not None or json_text
        temperature = self._temperature_for(role)
        route = self._route(role)
        failover_from: str | None = None
        last_err: Exception | None = None

        for target in route:
            provider, model, params = target.provider, target.model, target.params
            if provider == "fake":
                if self._fake is None:
                    continue
                if want_json and schema is not None:
                    obj = await self._fake.chat_json(role, messages, schema)
                    return obj.model_dump_json()
                return await self._fake.chat_text(role, messages, max_tokens or 256)

            timeout_s = target.timeout_s or self._timeout_for(role)
            if not self._provider_usable(provider, model, timeout_s):
                continue
            # After the key check, so a route whose paid target has no key behaves
            # exactly as before (no DB access, no marker).
            if target.budgeted and await self._skip_over_budget(role, target, failover_from):
                failover_from = target.spec
                continue

            cfg = self._providers[provider]

            if (
                self._cache is not None
                and temperature == 0.0
                and self.settings.llm_cache
            ):
                key = _cache_key(provider, model, messages, temperature, max_tokens, params)
                hit = self._cache.get(key)
                if hit is not None:
                    body, pt, ct = hit
                    await self._emit_call(
                        role,
                        provider,
                        model,
                        latency_ms=0,
                        failover_from=failover_from,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        cache_hit=True,
                    )
                    return body

            msgs = list(messages)
            if want_json and not cfg.json_mode:
                msgs = [
                    *msgs,
                    {"role": "system", "content": "Reply with JSON only. No markdown fences."},
                ]

            t0 = time.perf_counter()
            queue = QueueWait()
            call = self._call_anthropic if cfg.api == "anthropic" else self._call_chat
            try:
                body, pt, ct, latency_ms, key_id = await call(
                    role,
                    provider,
                    model,
                    msgs,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=want_json and cfg.json_mode,
                    timeout_s=timeout_s,
                    params=params,
                    queue=queue,
                    failover_from=failover_from,
                )
            except (_TargetExhausted, _TargetFailed) as e:
                # Only provider / HTTP / timeout failures fail over; anything else
                # raised by _call_chat is a bug and must surface.
                last_err = e
                await self._emit_call(
                    role,
                    provider,
                    model,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    failover_from=failover_from,
                    error=str(e),
                    queue_ms=queue.ms,
                    key_id=e.key_id,
                )
                failover_from = target.spec
                continue

            if self._cache is not None and temperature == 0.0 and self.settings.llm_cache:
                self._cache.put(
                    _cache_key(provider, model, messages, temperature, max_tokens, params),
                    body,
                    pt,
                    ct,
                )
            if target.budgeted:
                self._record_spend(target, messages, max_tokens, pt, ct)

            await self._emit_call(
                role,
                provider,
                model,
                latency_ms=latency_ms,
                failover_from=failover_from,
                prompt_tokens=pt,
                completion_tokens=ct,
                queue_ms=queue.ms,
                key_id=key_id,
            )
            return body

        raise LLMUnavailable(str(last_err) if last_err else "no usable LLM targets")

    def _disable(self, key: _ApiKey, status: int) -> None:
        if not key.disabled:
            key.disabled = True
            _log.warning("LLM key %s disabled for this process after HTTP %s", key.label, status)

    async def _on_pool(
        self,
        role: Role,
        provider: str,
        model: str,
        send: Callable[[Any, float], Awaitable[Any]],
        *,
        timeout_s: float,
        cost: int,
        queue: QueueWait,
        failover_from: str | None,
    ) -> tuple[Any, float, _ApiKey]:
        """Run one target on its key pool under one ``timeout_s`` deadline.

        ``send(client, remaining_s)`` makes the request. Returns ``(response,
        latency_ms of the last request, key)``. Raises ``_TargetExhausted`` when
        every live key is cooling longer than a short wait the deadline allows,
        ``_TargetFailed`` on timeout, every key disabled, 5xx after retries, other
        HTTP or connection errors. Messages are redacted and not chained, so no
        provider error body (which may echo a key) travels with them.
        """
        backoff_s = [1.0, 2.0, 4.0]
        attempt_5xx = 0
        short_waits = 0
        key: _ApiKey | None = None
        retry_same = False
        # A key failure is emitted only once the call moves to a *different* key;
        # retrying the same key after a cooldown, or failing the target, is
        # covered by the caller's final row.
        pending: tuple[_ApiKey, str, float] | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    if not retry_same:
                        key = self._pick_key(provider, model, cost)
                        if key is None:
                            wait = self._soonest_cooldown_s(provider, model)
                            last = f" (last: {pending[1]})" if pending else ""
                            last_id = pending[0].label if pending else None
                            if wait is None:
                                raise _TargetFailed(
                                    f"{provider}/{model} every key disabled{last}", last_id
                                )
                            if (
                                wait <= _SHORT_RETRY_S
                                and wait < deadline - loop.time()
                                and short_waits < _MAX_SHORT_RETRIES
                            ):
                                short_waits += 1
                                await self._rate_wait(wait + 0.001, queue)
                                continue
                            raise _TargetExhausted(
                                f"{provider}/{model} rate limited on every key{last}", last_id
                            )
                        if pending is not None and pending[0] is not key:
                            await self._emit_call(
                                role,
                                provider,
                                model,
                                latency_ms=pending[2],
                                failover_from=failover_from,
                                error=pending[1],
                                key_id=pending[0].label,
                            )
                            pending = None
                    retry_same = False
                    assert key is not None
                    limiter = self._limiter(provider, key.suffix, model)
                    budget = self._tpm_budget(provider, key.suffix, model) if cost > 0 else None
                    t_q = time.perf_counter()
                    try:
                        await limiter.acquire()
                        if budget is not None:
                            await budget.acquire(cost)
                    finally:
                        queue.ms += (time.perf_counter() - t_q) * 1000.0
                    t0 = time.perf_counter()
                    try:
                        resp = await send(key.client, max(0.001, deadline - loop.time()))
                    except _STATUS_ERRORS as e:
                        latency_ms = (time.perf_counter() - t0) * 1000.0
                        status = e.status_code
                        where = f"{provider}/{model} {key.label}"
                        quota400 = self._is_gemini_quota_400(provider, e)
                        if status == 429 or quota400:
                            self._cool(key, model, self._quota_cooldown_s(e))
                            if budget is not None:
                                budget.reconcile(cost, 0)  # rejected: not billed
                            what = "gemini quota (400→429)" if quota400 else "429 rate limited"
                            pending = (key, self._redact(f"{where} {what}: {e}"), latency_ms)
                            continue
                        if status in (401, 403):
                            self._disable(key, status)
                            pending = (key, self._redact(f"{where} HTTP {status}: {e}"), latency_ms)
                            continue
                        if 500 <= status < 600 and attempt_5xx < len(backoff_s):
                            delay = backoff_s[attempt_5xx] * (0.5 + random.random())
                            attempt_5xx += 1
                            await asyncio.sleep(delay)
                            retry_same = True
                            continue
                        if 500 <= status < 600:
                            msg = f"{where} 5xx exhausted retries: {e}"
                        else:
                            msg = f"{where} HTTP {status}: {e}"
                        raise _TargetFailed(self._redact(msg), key.label) from None
                    except _CONNECTION_ERRORS as e:
                        # Includes APITimeoutError (SDK-side timeout).
                        raise _TargetFailed(
                            self._redact(f"{provider}/{model} {key.label} connection: {e}"),
                            key.label,
                        ) from None
                    return resp, (time.perf_counter() - t0) * 1000.0, key
        except TimeoutError:
            raise _TargetFailed(
                f"{provider}/{model} timed out after {timeout_s}s",
                key.label if key is not None else None,
            ) from None

    async def _call_chat(
        self,
        role: Role,
        provider: str,
        model: str,
        messages: list[Mapping[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
        timeout_s: float,
        params: Mapping[str, Any] | None = None,
        queue: QueueWait | None = None,
        failover_from: str | None = None,
    ) -> tuple[str, int | None, int | None, float, str]:
        """One chat target on its key pool; raises ``_TargetExhausted`` / ``_TargetFailed``.

        Returns ``(text, prompt_tokens, completion_tokens, latency_ms, key label)``.
        The TPM budget is charged with a prompt estimate and reconciled from usage.
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if params:
            # extra_body: provider-specific knobs (reasoning_effort, …)
            # the SDK signature may not know.
            kwargs["extra_body"] = dict(params)

        async def send(client: AsyncOpenAI, remaining_s: float) -> Any:
            # SDK timeout covers real sockets; the pool deadline also bounds
            # limiter waits and transports that ignore httpx timeouts.
            return await client.chat.completions.create(**kwargs, timeout=remaining_s)

        cost = _estimate_tokens(messages)
        resp, latency_ms, key = await self._on_pool(
            role,
            provider,
            model,
            send,
            timeout_s=timeout_s,
            cost=cost,
            queue=queue if queue is not None else QueueWait(),
            failover_from=failover_from,
        )
        usage = resp.usage
        pt = usage.prompt_tokens if usage else None
        ct = usage.completion_tokens if usage else None
        budget = self._tpm_budget(provider, key.suffix, model)
        if budget is not None and pt is not None:
            budget.reconcile(cost, pt + (ct or 0))
        if not resp.choices:
            raise _TargetFailed(f"{provider}/{model} {key.label} returned no choices", key.label)
        choice = resp.choices[0].message.content or ""
        return choice, pt, ct, latency_ms, key.label

    async def _call_anthropic(
        self,
        role: Role,
        provider: str,
        model: str,
        messages: list[Mapping[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
        timeout_s: float,
        params: Mapping[str, Any] | None = None,
        queue: QueueWait | None = None,
        failover_from: str | None = None,
    ) -> tuple[str, int | None, int | None, float, str]:
        """One Anthropic Messages API target on its key pool; same contract as ``_call_chat``.

        ``temperature`` and ``json_mode`` are accepted for signature parity and
        ignored: sampling stays at the model default and JSON was already asked
        for by the system line ``_chat`` adds (providers with ``api: anthropic``
        never have ``json_mode``). Tokens are ``usage.input_tokens`` (+ cache
        read / write) and ``usage.output_tokens``.
        """
        system = "\n\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        convo = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in messages
            if m.get("role") != "system"
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or _ANTHROPIC_DEFAULT_MAX_TOKENS,
            "messages": convo,
        }
        if system:
            kwargs["system"] = system
        if params:
            kwargs["extra_body"] = dict(params)

        async def send(client: anthropic.AsyncAnthropic, remaining_s: float) -> Any:
            return await client.messages.create(**kwargs, timeout=remaining_s)

        resp, latency_ms, key = await self._on_pool(
            role,
            provider,
            model,
            send,
            timeout_s=timeout_s,
            cost=_estimate_tokens(messages),
            queue=queue if queue is not None else QueueWait(),
            failover_from=failover_from,
        )
        usage = resp.usage
        pt = ct = None
        if usage is not None:
            pt = (
                usage.input_tokens
                + (getattr(usage, "cache_read_input_tokens", None) or 0)
                + (getattr(usage, "cache_creation_input_tokens", None) or 0)
            )
            ct = usage.output_tokens
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        where = f"{provider}/{model} {key.label}"
        # Fail the target rather than hand back an empty or cut-off verdict.
        if resp.stop_reason == "refusal":
            raise _TargetFailed(f"{where} stop_reason=refusal", key.label)
        if resp.stop_reason == "max_tokens" and not text.strip():
            raise _TargetFailed(f"{where} stop_reason=max_tokens with no text", key.label)
        return text, pt, ct, latency_ms, key.label

    async def transcribe(self, wav_bytes: bytes, prompt: str | None = None) -> str:
        if self._fake is not None and self.settings.llm_profile == "offline":
            return await self._fake.transcribe(wav_bytes, prompt)

        route = self._route("stt")
        failover_from: str | None = None
        last_err: Exception | None = None
        use_prompt = prompt if prompt is not None else _WHISPER_PROMPT

        for target in route:
            provider, model = target.provider, target.model
            timeout_s = target.timeout_s or self._timeout_for("stt")
            if not self._provider_usable(provider, model, timeout_s):
                continue
            t0 = time.perf_counter()
            queue = QueueWait()
            try:
                text, latency_ms, key_id = await self._transcribe_once(
                    provider, model, wav_bytes, use_prompt,
                    params=target.params, queue=queue, timeout_s=timeout_s,
                    failover_from=failover_from,
                )
            except (_TargetExhausted, _TargetFailed) as e:
                last_err = e
                await self._emit_call(
                    "stt",
                    provider,
                    model,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    failover_from=failover_from,
                    error=str(e) or type(e).__name__,
                    queue_ms=queue.ms,
                    key_id=e.key_id,
                )
                failover_from = target.spec
                continue
            await self._emit_call(
                "stt",
                provider,
                model,
                latency_ms=latency_ms,
                failover_from=failover_from,
                queue_ms=queue.ms,
                key_id=key_id,
            )
            return text

        raise LLMUnavailable(str(last_err) if last_err else "no usable STT targets")

    async def _transcribe_once(
        self,
        provider: str,
        model: str,
        wav_bytes: bytes,
        prompt: str,
        *,
        params: Mapping[str, Any] | None = None,
        queue: QueueWait | None = None,
        timeout_s: float | None = None,
        failover_from: str | None = None,
    ) -> tuple[str, float, str]:
        """One STT target on its key pool, under one STT deadline; returns ``(text, ms, key)``."""
        timeout_s = timeout_s or self._timeout_for("stt")
        if self._providers[provider].api == "anthropic":
            raise _TargetFailed(f"{provider}/{model} has no speech-to-text API")

        async def send(client: AsyncOpenAI, remaining_s: float) -> Any:
            # The SDK wants a named file-like object; a fresh one per attempt.
            bio = io.BytesIO(wav_bytes)
            bio.name = "audio.wav"
            return await client.audio.transcriptions.create(
                model=model,
                file=bio,
                language="en",
                temperature=0,
                prompt=prompt,
                timeout=remaining_s,
                **({"extra_body": dict(params)} if params else {}),
            )

        resp, latency_ms, key = await self._on_pool(
            "stt",
            provider,
            model,
            send,
            timeout_s=timeout_s,
            cost=0,
            queue=queue if queue is not None else QueueWait(),
            failover_from=failover_from,
        )
        text = resp.text if hasattr(resp, "text") else str(resp)
        return text, latency_ms, key.label

    async def aclose(self) -> None:
        for keys in self._keys.values():
            for k in keys:
                await k.client.close()
        if self._cache is not None:
            self._cache.close()
        if self._budget is not None:
            self._budget.close()


class _TargetError(Exception):
    """Target-level failure; ``key_id`` is the label of the last key tried (or None)."""

    def __init__(self, message: str, key_id: str | None = None) -> None:
        super().__init__(message)
        self.key_id = key_id


class _TargetExhausted(_TargetError):
    """Every key of the target is rate limited; caller should fail over."""


class _TargetFailed(_TargetError):
    """Hard failure on target after retries; caller should fail over."""


def _check_anthropic_params(where: str, params: Mapping[str, Any]) -> None:
    """Reject route params that an Anthropic target must never send (see module docstring)."""
    bad = sorted(set(params) & _ANTHROPIC_BANNED_PARAMS)
    thinking = params.get("thinking")
    if isinstance(thinking, Mapping) and thinking.get("type") in ("disabled", "enabled"):
        bad.append(f"thinking.type={thinking.get('type')}")
    if bad:
        raise ValueError(f"anthropic route {where}: unsupported params {bad}")


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
