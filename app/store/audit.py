"""Append-only audit log for call events (belief changes, blocks, LLM calls).

One SQLite table ``events`` in WAL mode. BEFORE UPDATE / BEFORE DELETE triggers
abort so the log cannot be rewritten. Orchestrator and guards call ``append``;
eval and the UI read via ``for_call``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    call_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    payload JSON
);
"""

_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
"""


class AuditLog:
    """SQLite-backed append-only event log."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # FastAPI / TestClient may touch the connection from worker threads.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA + _TRIGGERS)
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(
        self,
        call_id: str,
        actor: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Insert one event; returns the new row id."""
        ts = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, call_id, actor, type, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    ts,
                    call_id,
                    actor,
                    event_type,
                    json.dumps(payload) if payload is not None else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def for_call(self, call_id: str) -> list[dict[str, Any]]:
        """Return all events for ``call_id`` in insertion order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, call_id, actor, type, payload FROM events "
                "WHERE call_id = ? ORDER BY id ASC",
                (call_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "call_id": row["call_id"],
                    "actor": row["actor"],
                    "type": row["type"],
                    "payload": payload,
                }
            )
        return out

    def list_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        """Summarize recent calls (newest first) for the operator UI."""
        with self._lock:
            ids = self._conn.execute(
                "SELECT call_id, MIN(ts) AS started_at, MAX(id) AS last_id "
                "FROM events GROUP BY call_id ORDER BY last_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in ids:
            call_id = str(row["call_id"])
            events = self.for_call(call_id)
            scenario_id = None
            phase = None
            turns = 0
            for ev in events:
                payload = ev.get("payload") or {}
                if ev["type"] == "call_started":
                    scenario_id = payload.get("scenario_id")
                if ev["type"] == "effects_committed":
                    phase = payload.get("phase") or phase
                if ev["type"] == "turn_complete":
                    turns += 1
                if ev["actor"] == "creditor" and ev["type"] == "utterance":
                    turns = max(turns, int(payload.get("turn") or 0))
            out.append(
                {
                    "call_id": call_id,
                    "started_at": row["started_at"],
                    "scenario_id": scenario_id,
                    "phase": phase,
                    "turn_count": turns,
                }
            )
        return out

    def export_call(self, call_id: str) -> dict[str, Any]:
        """Bundle transcript + events for download."""
        events = self.for_call(call_id)
        transcript: list[dict[str, Any]] = []
        for ev in events:
            payload = ev.get("payload") or {}
            if ev["actor"] == "creditor" and ev["type"] == "utterance":
                transcript.append(
                    {
                        "role": "creditor",
                        "text": payload.get("text"),
                        "turn": payload.get("turn"),
                        "ts": ev["ts"],
                    }
                )
            if ev["type"] == "turn_complete":
                for sentence in payload.get("sentences") or []:
                    transcript.append(
                        {
                            "role": "agent",
                            "text": sentence,
                            "intent": payload.get("intent"),
                            "ts": ev["ts"],
                        }
                    )
            if ev["type"] == "start":
                for sentence in payload.get("sentences") or []:
                    transcript.append(
                        {
                            "role": "agent",
                            "text": sentence,
                            "intent": payload.get("intent"),
                            "ts": ev["ts"],
                        }
                    )
        scenario_id = None
        for ev in events:
            if ev["type"] == "call_started":
                scenario_id = (ev.get("payload") or {}).get("scenario_id")
                break
        return {
            "call_id": call_id,
            "scenario_id": scenario_id,
            "transcript": transcript,
            "events": events,
        }
