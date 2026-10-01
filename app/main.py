"""FastAPI entrypoint: static UI, call WebSocket, metrics, call/scenario APIs.

Serves ``app/static`` at ``/``, mounts ``/ws/call/{call_id}``, and exposes
``GET /metrics/summary``, ``/scenarios``, and ``/calls*``. LLM + audit are
created once in lifespan and shared across sockets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.domain.scenario import list_scenario_metas, load_rep_card
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
        client = llm if llm is not None else make_client(cfg)
        owns_audit = audit is None
        owns_llm = llm is None
        voice_ws.configure(audit=log, llm=client, settings=cfg)
        app.state.audit = log
        app.state.llm = client
        app.state.settings = cfg
        try:
            yield
        finally:
            if owns_llm and hasattr(client, "aclose"):
                await client.aclose()
            if owns_audit:
                log.close()

    application = FastAPI(title="Debt Settlement Agent", lifespan=lifespan)
    application.include_router(voice_ws.router)

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

    @application.get("/scenarios/{scenario_id}/rep_card")
    async def get_rep_card(scenario_id: str) -> dict[str, str]:
        """Markdown cheat sheet for the human playing the creditor."""
        try:
            body = load_rep_card(scenario_id)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"id": scenario_id, "markdown": body}

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
        return FileResponse(_STATIC / "index.html")

    application.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return application


app = create_app()
