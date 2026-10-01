"""FastAPI entrypoint: static UI, call WebSocket, metrics summary.

Serves ``app/static`` at ``/``, mounts ``/ws/call/{call_id}``, and exposes
``GET /metrics/summary`` (p50/p95 per stage from the in-process latency buffer).
LLM + audit are created once in lifespan and shared across sockets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
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

    @application.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    application.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return application


app = create_app()
