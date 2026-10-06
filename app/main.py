"""FastAPI entrypoint: static UI, call WebSocket, metrics, call/scenario APIs.

Serves ``app/static`` at ``/``, mounts ``/ws/call/{call_id}``, and exposes
``GET /healthz`` (keep-warm), ``GET /metrics/summary``, ``/scenarios``
(+ ``/{id}`` operator brief and ``/{id}/rep_card``), and ``/calls*``. LLM + audit are
created once in lifespan and shared across sockets; the LLM's ``on_call`` hook
appends every LLM / STT call to the audit log (``app.llm.call_audit``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.domain.scenario import (
    SCENARIO_TEMPLATE,
    details_from_scenario,
    list_scenario_metas,
    load_rep_card,
    scenario_details,
    scenario_from_payload,
)
from app.llm.call_audit import audit_llm_calls
from app.llm.client import make_client
from app.store.audit import AuditLog
from app.voice import ws as voice_ws
from app.voice.metrics_buf import metrics_summary

_STATIC = Path(__file__).resolve().parent / "static"


def create_app(
    *,
    settings: Settings | None = None,
    llm: Any | None = None,
    audit: AuditLog | None = None,
) -> FastAPI:
    """Build the ASGI app; tests pass offline settings / FakeLLM / temp audit."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        log = audit or AuditLog(cfg.db_path)
        owns_audit = audit is None
        owns_llm = llm is None
        if owns_llm:
            client = make_client(cfg, on_call=audit_llm_calls(log))
        else:
            client = llm
            prior_hook = getattr(client, "on_call", None)
            client.on_call = audit_llm_calls(log, then=prior_hook)
        voice_ws.configure(audit=log, llm=client, settings=cfg)
        app.state.audit = log
        app.state.llm = client
        app.state.settings = cfg
        try:
            yield
        finally:
            if owns_llm and hasattr(client, "aclose"):
                await client.aclose()
            elif not owns_llm:
                client.on_call = prior_hook
            if owns_audit:
                log.close()

    application = FastAPI(title="Debt Settlement Agent", lifespan=lifespan)
    application.include_router(voice_ws.router)

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness for keep-warm pings; no LLM, engine or audit access."""
        return {"status": "ok"}

    @application.get("/metrics/summary")
    async def get_metrics_summary() -> dict[str, Any]:
        """Per-stage latency percentiles from completed turns in this process."""
        return metrics_summary()

    @application.get("/scenarios")
    async def get_scenarios() -> list[dict[str, str]]:
        """Catalog of curated demo cases for the operator picker."""
        return [
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "expected": m.expected,
            }
            for m in list_scenario_metas()
        ]

    @application.get("/scenarios/template")
    async def get_scenario_template() -> dict[str, Any]:
        """JSON template for a custom operator test case (client/offer/firm/meta)."""
        return SCENARIO_TEMPLATE

    @application.post("/scenarios/preview")
    async def preview_scenario(payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a custom test-case JSON and return the operator brief shape."""
        try:
            sc = scenario_from_payload(payload, rebase_to=date.today())
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        meta = payload.get("meta") or {}
        return details_from_scenario(
            sc,
            title=str(meta.get("title") or sc.id),
            description=str(meta.get("description") or ""),
            expected=str(meta.get("expected") or "deal"),
        )

    @application.get("/scenarios/{scenario_id}/rep_card")
    async def get_rep_card(scenario_id: str) -> dict[str, str]:
        """Markdown cheat sheet for the human playing the creditor."""
        try:
            body = load_rep_card(scenario_id)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"id": scenario_id, "markdown": body}

    @application.get("/scenarios/{scenario_id}")
    async def get_scenario(scenario_id: str) -> dict[str, Any]:
        """Operator brief: meta, creditor, PRIVATE client finances, firm fees."""
        try:
            return scenario_details(scenario_id, rebase_to=date.today())
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @application.get("/calls")
    async def get_calls(request: Request, limit: int = 50) -> list[dict[str, Any]]:
        """Recent call summaries from the append-only audit log."""
        log: AuditLog = request.app.state.audit
        return log.list_calls(limit=min(max(limit, 1), 200))

    @application.get("/calls/{call_id}/events")
    async def get_call_events(call_id: str, request: Request) -> list[dict[str, Any]]:
        log: AuditLog = request.app.state.audit
        return log.for_call(call_id)

    @application.get("/calls/{call_id}/export")
    async def export_call(call_id: str, request: Request) -> JSONResponse:
        log: AuditLog = request.app.state.audit
        payload = log.export_call(call_id)
        if not payload["events"]:
            raise HTTPException(status_code=404, detail="call not found")
        return JSONResponse(payload)

    @application.get("/")
    async def index() -> FileResponse:
        # Avoid sticky HTML that still references removed static assets (e.g. mock).
        return FileResponse(
            _STATIC / "index.html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    application.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return application


app = create_app()
