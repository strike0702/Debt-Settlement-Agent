"""Daily Deepgram voice budget and cost estimates for the Pipecat path.

``VoiceBudget`` keeps one row per UTC day in the ``voice_daily_spend`` table of
the app DB (``Settings.db_path``), like ``app.llm.budget.DailyBudget`` does for
paid NLU, so a restart does not reset the day. Money is integer micro-dollars.
``admit()`` is asked before a voice session starts and refuses once today's
spend reaches the cap (fail closed when the table is unreadable); ``add()``
records a finished session's Flux seconds and Aura-2 characters. A session that
starts under the cap runs to its end: the cap is checked per session, not per
frame. Prices come from ``Settings`` (``deepgram_*``); this module calls nothing.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.llm.budget import usd_to_micros, utc_now

_log = logging.getLogger(__name__)


def stt_cost_micros(seconds: Decimal, usd_per_min: Decimal) -> int:
    """Flux streaming cost of ``seconds`` of session audio (rounded up)."""
    return usd_to_micros(seconds * usd_per_min / 60)


def tts_cost_micros(chars: int, usd_per_1k_chars: Decimal) -> int:
    """Aura-2 cost of ``chars`` characters (rounded up)."""
    return usd_to_micros(Decimal(chars) * usd_per_1k_chars / 1000)


class VoiceBudget:
    """Persisted per-UTC-day Deepgram spend against ``limit_micros``."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        limit_micros: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.db_path = Path(db_path)
        self.limit_micros = limit_micros
        self._clock = clock
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, timeout=2.0)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_daily_spend (
                    day TEXT PRIMARY KEY,
                    spent_micros INTEGER NOT NULL DEFAULT 0,
                    sessions INTEGER NOT NULL DEFAULT 0,
                    stt_seconds TEXT NOT NULL DEFAULT '0',
                    tts_chars INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def today(self) -> date:
        """The current UTC day (the budget resets at 00:00 UTC)."""
        return self._clock().astimezone(UTC).date()

    def spent_micros(self) -> int:
        """Micro-dollars recorded today."""
        row = self._db().execute(
            "SELECT spent_micros FROM voice_daily_spend WHERE day = ?", (self.today().isoformat(),)
        ).fetchone()
        return int(row[0]) if row else 0

    def admit(self) -> bool:
        """True when a new voice session may start (today's spend under the cap)."""
        try:
            return self.spent_micros() < self.limit_micros
        except sqlite3.Error as e:
            _log.warning("voice budget table unreadable (%s); refusing voice", e)
            return False

    def add(self, micros: int, *, stt_seconds: Decimal, tts_chars: int) -> None:
        """Record one finished session's cost and usage."""
        day = self.today().isoformat()
        try:
            conn = self._db()
            row = conn.execute(
                "SELECT stt_seconds FROM voice_daily_spend WHERE day = ?", (day,)
            ).fetchone()
            secs = (Decimal(row[0]) if row else Decimal(0)) + stt_seconds
            conn.execute(
                """
                INSERT INTO voice_daily_spend (day, spent_micros, sessions, stt_seconds, tts_chars)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    spent_micros = spent_micros + excluded.spent_micros,
                    sessions = sessions + 1,
                    stt_seconds = excluded.stt_seconds,
                    tts_chars = tts_chars + excluded.tts_chars
                """,
                (day, micros, str(secs), tts_chars),
            )
            conn.commit()
        except sqlite3.Error as e:
            _log.warning("voice spend not recorded (%s)", e)

    def close(self) -> None:
        """Close the DB connection if open."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
