"""Phase 41: Claude Sonnet NLU on the demo under a daily budget.

Hermetic: fake keys, ``MockTransport`` for both SDKs, a temp SQLite budget DB
and an injected clock. Covers the shipped route shape, under / at / over budget,
UTC day rollover, spend surviving a new client, no-key behaviour (identical to
before, no DB touched), Anthropic failure → fallback and not counted, the eval
``judge`` role never being budgeted, and the audit rows the markers produce.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient
from pydantic import BaseModel

from app.config import Settings
from app.llm.budget import DailyBudget, ModelPrice, usd_to_micros
from app.llm.call_audit import audit_llm_calls, llm_call_scope
from app.llm.client import LLMClient, parse_route_entry
from app.store.audit import AuditLog

_REPO = Path(__file__).resolve().parents[2]
_MSG = [{"role": "system", "content": "Read the rep."}, {"role": "user", "content": "Hi."}]
_SONNET = {"target": "anthropic/claude-sonnet-5-5", "params": {"output_config": {"effort": "low"}}}

_YAML = """
profiles:
  demo:
    nlu:
      - target: anthropic/claude-sonnet-5-5
        params: {output_config: {effort: low}}
        budgeted: true
      - free/m
  eval:
    nlu: [free/m]
    judge:
      - {target: anthropic/claude-sonnet-5-5, params: {output_config: {effort: low}}}
providers:
  anthropic:
    api: anthropic
    base_url: "https://api.anthropic.test"
    key_env: ANTHROPIC_API_KEY
    rpm: 600
    prices_usd_per_mtok:
      claude-sonnet-5-5: {input: "2.00", output: "10.00"}
  free:
    base_url: "https://free.test/v1"
    key_env: GROQ_API_KEY
    rpm: 600
"""
# 1000 input × $2/M + 100 output × $10/M = 2000 + 1000 micro-dollars.
_CALL_MICROS = 3000


class _Out(BaseModel):
    who: str


def _anthropic_ok() -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": '{"who": "claude"}'}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    }


def _free_ok() -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"who": "free"}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 23, 59, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class _Rig:
    """One client over the test YAML with call counters per provider."""

    def __init__(
        self,
        tmp: Path,
        *,
        key: str | None = "sk-ant-test-1",
        profile: str = "demo",
        anthropic: Any = None,
        budget: DailyBudget | None = None,
        **settings_kw: Any,
    ) -> None:
        path = tmp / "providers.yaml"
        path.write_text(_YAML)
        self.anthropic_calls = 0
        self.free_calls = 0
        handler = anthropic or (lambda r: httpx2.Response(200, json=_anthropic_ok()))

        async def ah(request: httpx2.Request) -> httpx2.Response:
            self.anthropic_calls += 1
            out = handler(request)
            if asyncio.iscoroutine(out):
                out = await out
            return out

        def fh(request: httpx.Request) -> httpx.Response:
            self.free_calls += 1
            return httpx.Response(200, json=_free_ok())

        kw: dict[str, Any] = {
            "groq_api_key": "free-key",
            "mistral_api_key": None,
            "gemini_api_key": None,
            "anthropic_api_key": key,
            "llm_profile": profile,
            "llm_cache": False,
            "llm_cache_path": str(tmp / "cache.db"),
            "db_path": str(tmp / "app.db"),
            "api_key_pool": {},
        }
        kw.update(settings_kw)
        self.metas: list[dict[str, Any]] = []
        self.client = LLMClient(
            Settings(**kw),
            providers_path=path,
            http_clients={
                "anthropic": DefaultAsyncHttpxClient(transport=httpx2.MockTransport(ah)),
                "free": httpx.AsyncClient(transport=httpx.MockTransport(fh)),
            },
            on_call=self.metas.append,
            skip_health_check=True,
            budget=budget,
        )

    async def nlu(self) -> str:
        return (await self.client.chat_json("nlu", _MSG, _Out)).who

    def events(self) -> list[str | None]:
        return [m.get("event") for m in self.metas]


def _budget(tmp: Path, clock: _Clock, usd: str = "1.00") -> DailyBudget:
    return DailyBudget(tmp / "app.db", limit_micros=usd_to_micros(usd), clock=clock)


# --- shipped config -------------------------------------------------------


def test_shipped_demo_nlu_is_sonnet_first_then_the_old_free_chain() -> None:
    import yaml

    data = yaml.safe_load((_REPO / "config" / "providers.yaml").read_text())
    nlu = [parse_route_entry(e) for e in data["profiles"]["demo"]["nlu"]]
    first = nlu[0]
    assert (first.provider, first.model) == ("anthropic", "claude-sonnet-5-5")
    assert first.params == {"output_config": {"effort": "low"}}
    assert first.budgeted is True
    assert first.timeout_s is None  # the 6 s NLU role timeout applies
    assert [t.spec for t in nlu[1:]] == [
        "groq/openai/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        "gemini/gemini-3.1-flash-lite",
        "mistral/mistral-small-latest",
    ]
    assert all(not t.budgeted for t in nlu[1:])
    # NLG and STT never reach Anthropic; no other profile is budgeted.
    for role in ("nlg", "stt"):
        assert all(
            parse_route_entry(e).provider != "anthropic" for e in data["profiles"]["demo"][role]
        )
    for pname, roles in data["profiles"].items():
        for role, entries in roles.items():
            if (pname, role) == ("demo", "nlu"):
                continue
            assert not any(parse_route_entry(e).budgeted for e in entries), (pname, role)
    price = data["providers"]["anthropic"]["prices_usd_per_mtok"]["claude-sonnet-5-5"]
    assert ModelPrice.from_config(price, "shipped") == ModelPrice(2_000_000, 10_000_000)


def test_settings_budget_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().claude_daily_budget_usd == Decimal("1.00")
    monkeypatch.setenv("CLAUDE_DAILY_BUDGET_USD", "0.25")
    assert Settings().claude_daily_budget_usd == Decimal("0.25")
    monkeypatch.setenv("CLAUDE_DAILY_BUDGET_USD", "-1")
    with pytest.raises(ValueError):
        Settings()


def test_money_helpers_are_integer_micros() -> None:
    assert usd_to_micros("1.00") == 1_000_000
    assert usd_to_micros(Decimal("0.0000001")) == 1  # rounds up
    with pytest.raises(TypeError):
        usd_to_micros(1.0)  # type: ignore[arg-type]
    price = ModelPrice(2_000_000, 10_000_000)
    assert price.cost_micros(1000, 100) == _CALL_MICROS
    assert price.cost_micros(1, 0) == 2
    assert ModelPrice(100_000, 500_000).cost_micros(1, 0) == 1  # 0.1 µ$ rounds up
    with pytest.raises(ValueError):
        ModelPrice.from_config({"input": 2.0, "output": "10"}, "x")  # float price


def test_route_and_yaml_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="budgeted"):
        parse_route_entry({**_SONNET, "budgeted": "yes"})
    unpriced = _YAML.replace(
        "    prices_usd_per_mtok:\n      claude-sonnet-5-5: {input: \"2.00\", output: \"10.00\"}\n",
        "",
    )
    p = tmp_path / "p.yaml"
    p.write_text(unpriced)
    with pytest.raises(ValueError, match="prices_usd_per_mtok"):
        LLMClient(Settings(api_key_pool={}, llm_cache=False), providers_path=p)
    # The eval judge has its own approval: it may never be budgeted.
    judged = _YAML.replace(
        "    judge:\n      - {target: anthropic/claude-sonnet-5-5, params: {output_config: "
        "{effort: low}}}",
        "    judge:\n      - {target: anthropic/claude-sonnet-5-5, budgeted: true}",
    )
    assert judged != _YAML
    p.write_text(judged)
    with pytest.raises(ValueError, match="judge may not be budgeted"):
        LLMClient(Settings(api_key_pool={}, llm_cache=False), providers_path=p)


# --- behaviour ------------------------------------------------------------


async def test_under_budget_uses_sonnet_and_records_spend(tmp_path: Path) -> None:
    clock = _Clock()
    rig = _Rig(tmp_path, budget=_budget(tmp_path, clock))
    assert await rig.nlu() == "claude"
    assert (rig.anthropic_calls, rig.free_calls) == (1, 0)
    assert rig.metas[-1]["provider"] == "anthropic" and rig.metas[-1]["error"] is None
    status = rig.client.budget_status()
    assert status["spent_usd"] == Decimal("0.003")
    assert status["remaining_usd"] == Decimal("0.997")
    assert status["day"] == "2026-10-08"
    await rig.client.aclose()


async def test_at_budget_falls_back_without_calling_anthropic(tmp_path: Path) -> None:
    clock = _Clock()
    # Exactly two calls' worth: the second call reaches the cap.
    rig = _Rig(tmp_path, budget=_budget(tmp_path, clock, usd="0.006"))
    assert [await rig.nlu() for _ in range(2)] == ["claude", "claude"]
    assert await rig.nlu() == "free"
    assert await rig.nlu() == "free"
    assert (rig.anthropic_calls, rig.free_calls) == (2, 2)
    # Exhausted is noted once per day; every skipped call leaves a marker.
    assert rig.events().count("llm_budget_exhausted") == 1
    assert rig.events().count("llm_budget_skip") == 2
    skip = next(m for m in rig.metas if m.get("event") == "llm_budget_skip")
    assert skip["provider"] == "anthropic" and skip["error"] == "daily budget exhausted"
    free = [m for m in rig.metas if m["provider"] == "free"]
    assert all(m["failover_from"] == "anthropic/claude-sonnet-5-5" for m in free)
    assert rig.client.budget_status()["remaining_usd"] == Decimal(0)
    await rig.client.aclose()


async def test_day_rollover_resets_the_budget(tmp_path: Path) -> None:
    clock = _Clock()
    rig = _Rig(tmp_path, budget=_budget(tmp_path, clock, usd="0.003"))
    assert await rig.nlu() == "claude"
    assert await rig.nlu() == "free"
    clock.now += timedelta(minutes=2)  # 00:01 UTC next day
    assert await rig.nlu() == "claude"
    assert rig.client.budget_status()["day"] == "2026-10-09"
    assert rig.anthropic_calls == 2
    await rig.client.aclose()


async def test_spend_survives_a_new_client(tmp_path: Path) -> None:
    clock = _Clock()
    first = _Rig(tmp_path, budget=_budget(tmp_path, clock, usd="0.003"))
    assert await first.nlu() == "claude"
    await first.client.aclose()
    # Fresh process: default budget built from Settings (db_path, env budget).
    second = _Rig(tmp_path, claude_daily_budget_usd=Decimal("0.003"))
    second.client._budget_for()._clock = clock
    assert await second.nlu() == "free"
    assert second.anthropic_calls == 0
    assert second.events().count("llm_budget_exhausted") == 1
    await second.client.aclose()


async def test_exhausted_is_noted_once_per_day_across_restarts(tmp_path: Path) -> None:
    clock = _Clock()
    for _ in range(2):
        rig = _Rig(tmp_path, budget=_budget(tmp_path, clock, usd="0"))
        assert await rig.nlu() == "free"
        events = rig.events()
        await rig.client.aclose()
    assert events.count("llm_budget_exhausted") == 0  # second process: already noted
    assert events.count("llm_budget_skip") == 1


async def test_no_key_behaves_as_before(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, key=None)
    assert await rig.nlu() == "free"
    assert rig.anthropic_calls == 0
    assert rig.metas == [m for m in rig.metas if m.get("event") is None]
    assert len(rig.metas) == 1 and rig.metas[0]["failover_from"] is None
    assert rig.client._budget is None
    assert not (tmp_path / "app.db").exists()
    await rig.client.aclose()


async def test_anthropic_error_falls_back_and_is_not_counted(tmp_path: Path) -> None:
    clock = _Clock()
    rig = _Rig(
        tmp_path,
        budget=_budget(tmp_path, clock),
        anthropic=lambda r: httpx2.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error"}}
        ),
    )
    assert await rig.nlu() == "free"
    assert rig.anthropic_calls == 1
    assert rig.metas[0]["provider"] == "anthropic" and rig.metas[0]["error"]
    assert rig.client.budget_status()["spent_usd"] == Decimal(0)
    await rig.client.aclose()


async def test_anthropic_timeout_falls_back_and_is_not_counted(tmp_path: Path) -> None:
    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(2)
        return httpx2.Response(200, json=_anthropic_ok())

    clock = _Clock()
    rig = _Rig(
        tmp_path, budget=_budget(tmp_path, clock), anthropic=slow, llm_timeout_nlu_s=0.2
    )
    assert await rig.nlu() == "free"
    assert "timed out" in rig.metas[0]["error"] or "connection" in rig.metas[0]["error"]
    assert rig.client.budget_status()["spent_usd"] == Decimal(0)
    await rig.client.aclose()


async def test_cache_hit_is_not_counted(tmp_path: Path) -> None:
    clock = _Clock()
    rig = _Rig(tmp_path, budget=_budget(tmp_path, clock), llm_cache=True)
    assert await rig.nlu() == "claude"
    assert await rig.nlu() == "claude"
    assert rig.anthropic_calls == 1 and rig.metas[-1]["cache_hit"] is True
    assert rig.client.budget_status()["spent_usd"] == Decimal("0.003")
    await rig.client.aclose()


async def test_judge_is_not_subject_to_the_demo_budget(tmp_path: Path) -> None:
    clock = _Clock()
    budget = _budget(tmp_path, clock, usd="0")
    rig = _Rig(tmp_path, profile="eval", budget=budget)
    assert budget.exhausted()
    out = await rig.client.chat_text("judge", _MSG, max_tokens=50)
    assert out == '{"who": "claude"}'
    assert rig.anthropic_calls == 1
    assert all(m.get("event") is None for m in rig.metas)
    assert budget.spent_micros() == 0  # judge spend is not demo spend
    await rig.client.aclose()


async def test_budget_markers_become_audit_rows(tmp_path: Path) -> None:
    clock = _Clock()
    audit = AuditLog(tmp_path / "audit.db")
    rig = _Rig(tmp_path, budget=_budget(tmp_path, clock, usd="0"))
    rig.client.on_call = audit_llm_calls(audit)
    with llm_call_scope("c1"):
        assert await rig.nlu() == "free"
    events = [(r["actor"], r["type"]) for r in audit.for_call("c1")]
    assert events == [
        ("llm", "llm_budget_exhausted"),
        ("llm", "llm_budget_skip"),
        ("llm", "llm_call"),
    ]
    exhausted = audit.for_call("c1")[0]["payload"]
    assert exhausted["limit_usd"] == "0" and exhausted["day"] == "2026-10-08"
    called = audit.for_call("c1")[2]["payload"]
    assert called["model"] == "m" and called["failover_from"] == "anthropic/claude-sonnet-5-5"
    await rig.client.aclose()
    audit.close()


async def test_unreadable_budget_fails_closed_to_the_free_chain(tmp_path: Path) -> None:
    clock = _Clock()
    budget = DailyBudget(tmp_path / "missing" / "app.db", limit_micros=10**6, clock=clock)
    rig = _Rig(tmp_path, budget=budget)
    assert await rig.nlu() == "free"
    assert rig.anthropic_calls == 0
    await rig.client.aclose()
