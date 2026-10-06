"""Attribute every LLM / STT call to a call id and append it to the audit log.

``LLM_CALL_ID`` is a ``contextvars.ContextVar`` holding the call id of the turn
in progress. ``Orchestrator`` sets it (via ``llm_call_scope``) around each public
turn method, so concurrent calls on one event loop never see each other's id.
The eval runner also sets it around the sim, so sim-phrasing calls are audited.

``audit_llm_calls`` builds an ``on_call`` hook for ``LLMClient`` / ``FakeLLM``
that writes one row per attempt: ``llm_call`` on success or cache hit,
``llm_call_failed`` when a target failed (``error`` set). Wired in the FastAPI
lifespan (``app.main``), ``app.cli``, and ``eval.run_eval``. Calls made with no
call id in context (e.g. the NLU corpus scorer) are not written anywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.llm.client import OnCallHook
from app.store.audit import AuditLog

LLM_CALL_ID: ContextVar[str | None] = ContextVar("llm_call_id", default=None)


@contextmanager
def llm_call_scope(call_id: str) -> Iterator[None]:
    """Attribute LLM calls made inside this block (and tasks it spawns) to ``call_id``."""
    token = LLM_CALL_ID.set(call_id)
    try:
        yield
    finally:
        LLM_CALL_ID.reset(token)


def audit_llm_calls(audit: AuditLog, *, then: OnCallHook | None = None) -> OnCallHook:
    """``on_call`` hook: append the meta to ``audit`` under ``LLM_CALL_ID``, then chain."""

    async def hook(meta: dict[str, Any]) -> None:
        call_id = LLM_CALL_ID.get()
        if call_id is not None:
            event = "llm_call_failed" if meta.get("error") else "llm_call"
            audit.append(call_id, "llm", event, dict(meta))
        if then is not None:
            result = then(meta)
            if asyncio.iscoroutine(result):
                await result

    return hook
