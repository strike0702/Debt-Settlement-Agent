"""Phase 41 follow-up: eval tooling never reaches a paid (``budgeted``) target by accident.

Hermetic: fake keys and mock transports for Anthropic and Groq. A default
``eval.nlu_corpus`` run on the ``demo`` profile must make no Anthropic call;
``allow_budgeted`` keeps the Claude target; an explicit providers file is used
as is. ``eval.run_eval`` picks the same providers file the same way, and its
call counter ignores budget markers.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import yaml
from anthropic import DefaultAsyncHttpxClient

import eval.nlu_corpus as nlu_corpus
import eval.run_eval as run_eval
from app.llm.client import LLMClient, parse_route_entry

_SHIPPED = Path(__file__).resolve().parents[2] / "config" / "providers.yaml"
_REPLY = '{"stance": "question", "asks_client_private_info": true}'
_LINE = {"id": "b01", "text": "What is the client's monthly income?", "stance": "question"}


def test_without_budgeted_drops_only_the_demo_claude_target(tmp_path: Path) -> None:
    path, removed = run_eval.without_budgeted(_SHIPPED, tmp_path)
    assert removed == ["demo/nlu/anthropic/claude-haiku-5-5"]
    assert path.parent == tmp_path
    before = yaml.safe_load(_SHIPPED.read_text())
    after = yaml.safe_load(path.read_text())
    assert [parse_route_entry(e).spec for e in after["profiles"]["demo"]["nlu"]] == [
        parse_route_entry(e).spec for e in before["profiles"]["demo"]["nlu"][1:]
    ]
    before["profiles"]["demo"]["nlu"] = before["profiles"]["demo"]["nlu"][1:]
    assert after == before  # judge routes, providers and prices untouched
    # Nothing budgeted → the source file itself, nothing written.
    assert run_eval.without_budgeted(path, tmp_path / "x") == (path, [])


class _Calls:
    def __init__(self) -> None:
        self.anthropic = 0
        self.groq = 0
        self.providers_path: Path | None = None


def _patch_client(monkeypatch: pytest.MonkeyPatch, tmp: Path) -> _Calls:
    """Replace ``make_client`` with a real LLMClient on mock transports and fake keys."""
    calls = _Calls()

    def anthropic(request: httpx2.Request) -> httpx2.Response:
        calls.anthropic += 1
        return httpx2.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": _REPLY}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    def groq(request: httpx.Request) -> httpx.Response:
        calls.groq += 1
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": _REPLY},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    def fake_make_client(settings: Any, **kw: Any) -> LLMClient:
        calls.providers_path = kw.get("providers_path")
        s = settings.model_copy(
            update={
                "anthropic_api_key": "sk-ant-fake",
                "groq_api_key": "groq-fake",
                "llm_cache": False,
                "db_path": str(tmp / "app.db"),
            }
        )
        return LLMClient(
            s,
            providers_path=kw.get("providers_path"),
            on_call=kw.get("on_call"),
            http_clients={
                "anthropic": DefaultAsyncHttpxClient(transport=httpx2.MockTransport(anthropic)),
                "groq": httpx.AsyncClient(transport=httpx.MockTransport(groq)),
            },
            skip_health_check=True,
        )

    monkeypatch.setattr(nlu_corpus, "make_client", fake_make_client)
    return calls


async def test_corpus_default_demo_run_makes_no_anthropic_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_client(monkeypatch, tmp_path)
    records, models = await nlu_corpus.run_corpus([_LINE], profile="demo")
    assert (calls.anthropic, calls.groq) == (0, 1)
    assert models == Counter({"groq/openai/gpt-oss-120b": 1})
    assert not records[0].get("skipped")
    out = capsys.readouterr().out
    assert out.count("skipped budgeted (paid) targets: demo/nlu/anthropic/claude-haiku-5-5") == 1


async def test_corpus_allow_budgeted_keeps_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_client(monkeypatch, tmp_path)
    _, models = await nlu_corpus.run_corpus([_LINE], profile="demo", allow_budgeted=True)
    assert (calls.anthropic, calls.groq) == (1, 0)
    assert models == Counter({"anthropic/claude-haiku-5-5": 1})
    assert calls.providers_path is None
    assert "skipped budgeted" not in capsys.readouterr().out


async def test_corpus_explicit_providers_file_is_used_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_client(monkeypatch, tmp_path)
    explicit = tmp_path / "approved.yaml"
    explicit.write_text(_SHIPPED.read_text())
    _, models = await nlu_corpus.run_corpus([_LINE], profile="demo", providers_path=explicit)
    assert calls.providers_path == explicit
    assert calls.anthropic == 1
    assert models == Counter({"anthropic/claude-haiku-5-5": 1})


def test_corpus_cli_has_allow_budgeted_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(corpus: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise SystemExit(0)

    monkeypatch.setattr(nlu_corpus, "run_corpus", fake_run)
    no_audit = type("A", (), {"close": lambda self: None})
    monkeypatch.setattr(nlu_corpus, "AuditLog", lambda path: no_audit())
    for argv, expected in (([], False), (["--allow-budgeted"], True)):
        with pytest.raises(SystemExit):
            nlu_corpus.main(["--label", "X", *argv])
        assert seen["allow_budgeted"] is expected


class _Stop(Exception):
    pass


@pytest.mark.parametrize(
    ("extra", "expect"),
    [
        ([], "stripped"),
        (["--allow-budgeted"], "shipped"),
        (["--providers", "EXPLICIT"], "explicit"),
    ],
)
def test_run_eval_providers_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    expect: str,
) -> None:
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(_SHIPPED.read_text())
    extra = [str(explicit) if a == "EXPLICIT" else a for a in extra]
    seen: dict[str, Any] = {}

    def fake_make_client(settings: Any, **kw: Any) -> Any:
        seen["path"] = Path(kw["providers_path"])
        raise _Stop

    monkeypatch.setattr(run_eval, "RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(run_eval, "make_client", fake_make_client)
    with pytest.raises(_Stop):
        run_eval.main(["--profile", "demo", "--nlu", "llm", "--scenarios", "1", *extra])
    path: Path = seen["path"]
    out = capsys.readouterr().out
    if expect == "stripped":
        assert path.name == "providers_no_budgeted.yaml"
        assert path.parent.parent == tmp_path / "results"
        routes = yaml.safe_load(path.read_text())["profiles"]["demo"]["nlu"]
        assert all(parse_route_entry(e).provider != "anthropic" for e in routes)
        assert "skipped budgeted (paid) targets" in out
    else:
        assert path == (_SHIPPED if expect == "shipped" else explicit)
        assert "skipped budgeted" not in out


def test_run_eval_skip_line_only_for_the_runs_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_make_client(settings: Any, **kw: Any) -> Any:
        raise _Stop

    monkeypatch.setattr(run_eval, "RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(run_eval, "make_client", fake_make_client)
    with pytest.raises(_Stop):
        run_eval.main(["--profile", "eval", "--nlu", "llm", "--scenarios", "1"])
    assert "skipped budgeted" not in capsys.readouterr().out


def test_run_eval_call_counter_ignores_cache_hits_and_budget_markers() -> None:
    counts: Counter[str] = Counter()
    base = {"provider": "groq", "model": "m", "cache_hit": False}
    run_eval.count_live_call(counts, base)
    run_eval.count_live_call(counts, {**base, "error": "boom"})  # failed attempt still counts
    run_eval.count_live_call(counts, {**base, "cache_hit": True})
    for event in ("llm_budget_skip", "llm_budget_exhausted"):
        run_eval.count_live_call(
            counts, {"provider": "anthropic", "model": "s", "cache_hit": False, "event": event}
        )
    assert counts == Counter({"groq/m": 2})
