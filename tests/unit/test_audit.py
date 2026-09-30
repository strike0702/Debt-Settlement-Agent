"""Audit log: WAL, append-only triggers, append / for_call."""

from __future__ import annotations

import sqlite3

import pytest

from app.store.audit import AuditLog


def test_append_and_for_call(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    id1 = log.append("c1", "agent", "belief_change", {"field": "max_payments"})
    id2 = log.append("c1", "system", "blocked", {"reason": "digit"})
    log.append("c2", "agent", "other", None)
    rows = log.for_call("c1")
    assert [r["id"] for r in rows] == [id1, id2]
    assert rows[0]["payload"] == {"field": "max_payments"}
    assert rows[1]["type"] == "blocked"
    log.close()


def test_update_raises_append_only(tmp_path) -> None:
    path = tmp_path / "audit.db"
    log = AuditLog(path)
    log.append("c1", "agent", "x", {})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log._conn.execute("UPDATE events SET type = 'y' WHERE id = 1")
        log._conn.commit()
    log.close()


def test_delete_raises_append_only(tmp_path) -> None:
    path = tmp_path / "audit.db"
    log = AuditLog(path)
    log.append("c1", "agent", "x", {})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log._conn.execute("DELETE FROM events WHERE id = 1")
        log._conn.commit()
    log.close()
