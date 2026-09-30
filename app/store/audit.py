"""Append-only audit log for call events (belief changes, blocks, LLM calls).

One SQLite table ``events`` in WAL mode. BEFORE UPDATE / BEFORE DELETE triggers
abort so the log cannot be rewritten. Orchestrator and guards call ``append``;
eval and the UI read via ``for_call``.
"""

from __future__ import annotations

import json
import sqlite3
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
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA + _TRIGGERS)
        self._conn.commit()

    def close(self) -> None:
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
