"""Phase 20 (F5, F17): every LLM call is audited under the turn's call id.

FakeLLM NLU / NLG calls made inside ``Orchestrator`` turns must land in
``AuditLog.for_call(call_id)``; the call id comes from the ``LLM_CALL_ID``
ContextVar, so two concurrent sessions never cross-attribute. NLG
``LLMUnavailable`` reaches the orchestrator fallback and is audited.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.config import Settings
from app.domain.scenario import load_scenario
from app.llm.call_audit import LLM_CALL_ID, audit_llm_calls, llm_call_scope
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog


def _settings(**kwargs: object) -> Settings:
    base: dict[str, object] = dict(
        nlu_mode="llm",
        nlg_mode="template",
        llm_profile="offline",
        llm_cache=False,
        firm_name="Synthetic Debt Relief",
        opening_disclosure="This call uses synthetic data for demonstration only.",
    )
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def _nlu_reply() -> dict[str, object]:
    return {"terms": [], "stance": "info"}


def _orch(audit: AuditLog, fake: FakeLLM, **kw: object) -> tuple[Orchestrator, CallSession]:
    session = CallSession(scenario=load_scenario("fixtures/demo"))
    orch = Orchestrator(session, llm=fake, settings=_settings(**kw), audit=audit, auto_ack=True)
    return orch, session


@pytest.mark.asyncio
async def test_fake_nlu_call_in_turn_lands_in_audit(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeLLM()
    fake.on_call = audit_llm_calls(audit)
    orch, session = _orch(audit, fake)
    await orch.start()
    fake.enqueue("nlu", _nlu_reply())
    await orch.on_creditor_text("Hello, which account is this about?")

    rows = [e for e in audit.for_call(session.call_id) if e["type"] == "llm_call"]
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert rows[0]["actor"] == "llm"
    for key in (
        "role",
        "provider",
        "model",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "cache_hit",
        "failover_from",
    ):
        assert key in payload
    assert payload["role"] == "nlu"
    assert payload["error"] is None
    # The ContextVar is reset after the turn.
    assert LLM_CALL_ID.get() is None
    audit.close()


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_cross_attribute(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeLLM()
    fake.on_call = audit_llm_calls(audit)
    orch_a, sess_a = _orch(audit, fake)
    orch_b, sess_b = _orch(audit, fake)
    await orch_a.start()
    await orch_b.start()
    for _ in range(2):
        fake.enqueue("nlu", _nlu_reply())
    await asyncio.gather(
        orch_a.on_creditor_text("Hello there."),
        orch_b.on_creditor_text("Good afternoon."),
    )
    for sess in (sess_a, sess_b):
        rows = [e for e in audit.for_call(sess.call_id) if e["type"] == "llm_call"]
        assert len(rows) == 1
    audit.close()


@pytest.mark.asyncio
async def test_failed_call_is_audited_and_unscoped_calls_are_not(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeLLM()
    fake.on_call = audit_llm_calls(audit)
    with pytest.raises(Exception):
        await fake.chat_text("nlu", [], 10)  # outside any call scope → not written
    with llm_call_scope("call-x"):
        with pytest.raises(Exception):
            await fake.chat_text("nlg", [], 10)
    rows = audit.for_call("call-x")
    assert [r["type"] for r in rows] == ["llm_call_failed"]
    assert rows[0]["payload"]["role"] == "nlg"
    assert "queue empty" in rows[0]["payload"]["error"]
    audit.close()


@pytest.mark.asyncio
async def test_nlg_llm_unavailable_reaches_orchestrator_fallback(tmp_path: Path) -> None:
    """F17: speak_action no longer swallows LLMUnavailable; orchestrator audits it."""
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeLLM()
    fake.on_call = audit_llm_calls(audit)
    orch, session = _orch(audit, fake, nlg_mode="llm")
    await orch.start()  # OPENING is template-only: no LLM call
    fake.enqueue("nlu", {"terms": [], "stance": "info", "asks_client_private_info": True})
    # REFUSE_PRIVATE goes through the LLM; no nlg reply queued → LLMUnavailable.
    utt = await orch.on_creditor_text("What is the client's monthly income?")
    assert utt.action.intent.value == "REFUSE_PRIVATE"
    assert utt.sentences  # fallback template still spoke

    types = [e["type"] for e in audit.for_call(session.call_id)]
    assert "llm_unavailable" in types
    assert "llm_call_failed" in types
    audit.close()


def test_app_lifespan_wires_llm_audit(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeLLM()
    app = create_app(settings=_settings(), llm=fake, audit=audit)
    with TestClient(app):
        assert fake.on_call is not None
    assert fake.on_call is None  # restored on shutdown
    audit.close()
