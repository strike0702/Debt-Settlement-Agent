"""FastAPI entrypoint: the built web UI, call WebSocket, metrics, call/scenario APIs.

Serves the Vite build in ``web/dist`` at ``/`` (SPA fallback: unknown non-API
paths get ``index.html`` with ``Cache-Control: no-cache``; hashed files under
``/assets`` are cached for a year). Mounts ``/ws/call/{call_id}``, and exposes
``GET /healthz`` (keep-warm), ``GET /metrics/summary``, ``/scenarios``
(+ ``/{id}`` operator brief, ``/{id}/rep_card``, and ``/{id}/rep``: the
creditor's own account and rules for the rep lens; ``/template`` and
``POST /preview`` + ``/preview/rep`` for custom test cases pasted in the UI),
and ``/calls*`` (``?view=rep`` drops private audit rows; ``/{id}/operator`` is
the in-memory decision trace of a call, whatever view it streamed). LLM +
audit are created once in lifespan and shared across sockets; the LLM's
``on_call`` hook appends every LLM / STT call to the audit log
(``app.llm.call_audit``). This module builds no UI: ``npm run build``
in ``web/`` does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from app.config import Settings, get_settings
from app.domain.scenario import (
    SCENARIO_TEMPLATE,
    details_from_scenario,
    list_scenario_metas,
    load_rep_card,
    rep_account,
    rep_account_from_payload,
    rep_card_suggestions,
    scenario_details,
    scenario_from_payload,
    scenario_payload_errors,
)
from app.llm.call_audit import audit_llm_calls
from app.llm.client import make_client
from app.schemas.events import VIEWS, View
from app.store.audit import AuditLog
from app.voice import ws as voice_ws
from app.voice.metrics_buf import metrics_summary
from app.voice.views import is_private_audit

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
# Paths the SPA fallback must never answer: an unknown API path stays a 404.
_API_PREFIXES = ("ws", "scenarios", "calls", "metrics", "healthz", "assets")
_NO_CACHE = {"Cache-Control": "no-cache"}
_IMMUTABLE = "public, max-age=31536000, immutable"


class _HashedAssets(StaticFiles):
    """Vite's ``/assets`` files have content hashes in their names: cache them for good."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = _IMMUTABLE
        return response


def _check_view(view: str) -> View:
    if view not in VIEWS:
        raise HTTPException(status_code=400, detail=f"unknown view: {view!r}")
    return view  # type: ignore[return-value]


def _rows_for_view(rows: list[dict[str, Any]], view: View) -> list[dict[str, Any]]:
    """Audit rows as ``view`` may see them; ``rep`` drops private rows (same rule as the WS)."""
    if view == "operator":
        return rows
    return [r for r in rows if not is_private_audit(str(r.get("actor")), str(r.get("type")))]


def create_app(
    *,
    settings: Settings | None = None,
    llm: Any | None = None,
    audit: AuditLog | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    """Build the ASGI app; tests pass offline settings / FakeLLM / temp audit / a fake dist."""
    dist = web_dist or WEB_DIST

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
    async def get_scenarios() -> list[dict[str, Any]]:
        """Catalog of curated demo cases; ``suggested`` = the rep card's suggested replies."""
        return [
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "expected": m.expected,
                "suggested": rep_card_suggestions(load_rep_card(m.id)),
            }
            for m in list_scenario_metas()
        ]

    @application.get("/scenarios/template")
    async def get_scenario_template() -> dict[str, Any]:
        """JSON template for a custom operator test case (client/offer/firm/meta)."""
        return SCENARIO_TEMPLATE

    @application.post("/scenarios/preview")
    async def preview_scenario(payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a custom test case and return its operator brief.

        400 ``detail = {message, errors: [{path, message}]}`` lists every problem
        with its field path so the editor can show them next to the JSON.
        """
        errors = scenario_payload_errors(payload)
        if not errors:
            try:
                sc = scenario_from_payload(payload, rebase_to=date.today())
            except (ValueError, TypeError, KeyError) as e:
                errors = [{"path": "", "message": f"The engine rejected this case: {e}"}]
        if errors:
            raise HTTPException(
                status_code=400, detail={"message": errors[0]["message"], "errors": errors}
            )
        meta = payload.get("meta") or {}
        return details_from_scenario(
            sc,
            title=str(meta.get("title") or sc.id),
            description=str(meta.get("description") or ""),
            expected=str(meta.get("expected") or "deal"),
        )

    @application.post("/scenarios/preview/rep")
    async def preview_scenario_rep(payload: dict[str, Any]) -> dict[str, Any]:
        """Rep lens "Your account" for a custom case: reads only ``offer`` and ``rep_card``."""
        return rep_account_from_payload(payload)

    @application.get("/scenarios/{scenario_id}/rep_card")
    async def get_rep_card(scenario_id: str) -> dict[str, str]:
        """Markdown cheat sheet for the human playing the creditor."""
        try:
            body = load_rep_card(scenario_id)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"id": scenario_id, "markdown": body}

    @application.get("/scenarios/{scenario_id}/rep")
    async def get_rep_account(scenario_id: str) -> dict[str, Any]:
        """Rep lens: the creditor's own account and settlement rules (no client data)."""
        try:
            return rep_account(scenario_id)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

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
    async def get_call_events(
        call_id: str, request: Request, view: str = "operator"
    ) -> list[dict[str, Any]]:
        """Audit rows for one call; ``view=rep`` drops private rows."""
        log: AuditLog = request.app.state.audit
        return _rows_for_view(log.for_call(call_id), _check_view(view))

    @application.get("/calls/{call_id}/operator")
    async def get_call_operator_detail(call_id: str) -> dict[str, Any]:
        """Debt negotiator detail of a call (traces, private audit, eval, agreement).

        Kept in memory per call whatever view the call streamed, so the UI can
        show the decision trace for a call started in the Creditor rep view.
        Same access model as ``/calls/{id}/events`` (operator by default).
        """
        detail = voice_ws.operator_detail(call_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="no detail for this call in memory")
        return detail

    @application.get("/calls/{call_id}/export")
    async def export_call(call_id: str, request: Request, view: str = "operator") -> JSONResponse:
        """Downloadable call log; ``view=rep`` drops private rows."""
        v = _check_view(view)
        log: AuditLog = request.app.state.audit
        payload = log.export_call(call_id)
        if not payload["events"]:
            raise HTTPException(status_code=404, detail="call not found")
        payload["events"] = _rows_for_view(payload["events"], v)
        payload["view"] = v
        return JSONResponse(payload)

    if (dist / "assets").is_dir():
        application.mount("/assets", _HashedAssets(directory=dist / "assets"), name="assets")

    @application.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        """Files at the dist root (favicon) as-is; any other non-API path is the SPA shell."""
        if path.split("/", 1)[0] in _API_PREFIXES:
            raise HTTPException(status_code=404)
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(
                "<p>The web UI is not built. Run <code>npm ci &amp;&amp; npm run build</code>"
                " in <code>web/</code>.</p>",
                status_code=503,
                headers=_NO_CACHE,
            )
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate, headers=_NO_CACHE)
        return FileResponse(index, headers=_NO_CACHE)

    return application


app = create_app()
