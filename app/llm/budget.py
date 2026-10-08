"""Daily spend cap for paid route targets (the demo's Claude NLU, Phase 41).

``DailyBudget`` keeps one row per UTC day in a small SQLite table
(``llm_daily_spend``) inside the app DB (``Settings.db_path``), so a process
restart does not reset the day's spend. Money is integer micro-dollars
(1 USD = 1_000_000); prices come from ``config/providers.yaml``
(``prices_usd_per_mtok`` on a provider), never from code.

It does not route or call anything. ``app.llm.client.LLMClient`` asks
``exhausted()`` before a route target marked ``budgeted: true`` (only the demo
``nlu`` route's Anthropic target) and calls ``record()`` after that target
answers live. Failed or timed-out attempts and cache hits are not recorded.
If the table cannot be read the budget counts as exhausted (fail closed: the
call falls back to the free chain, it is never blocked).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

MICROS_PER_USD = 1_000_000
_TOKENS_PER_MTOK = 1_000_000
_log = logging.getLogger(__name__)


def usd_to_micros(value: Decimal | str | int) -> int:
    """USD amount → integer micro-dollars (rounded up; floats are rejected)."""
    if isinstance(value, float):
        raise TypeError("money must not be a float; pass a Decimal, str or int")
    micros = Decimal(str(value)) * MICROS_PER_USD
    return int(micros.to_integral_value(rounding=ROUND_CEILING))


def micros_to_usd(micros: int) -> Decimal:
    """Integer micro-dollars → Decimal USD (exact)."""
    return Decimal(micros) / MICROS_PER_USD


@dataclass(frozen=True)
class ModelPrice:
    """Per-million-token prices in micro-dollars (input, output)."""

    input_micros_per_mtok: int
    output_micros_per_mtok: int

    @classmethod
    def from_config(cls, raw: Mapping[str, Any], where: str) -> ModelPrice:
        """Parse ``{input: "2.00", output: "10.00"}`` (USD per million tokens)."""
        try:
            return cls(usd_to_micros(raw["input"]), usd_to_micros(raw["output"]))
        except (KeyError, TypeError, ArithmeticError) as e:
            raise ValueError(
                f"{where}: price needs string/int 'input' and 'output' USD per MTok: {e}"
            ) from None

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        """Cost of one call in micro-dollars, rounded up to a whole micro-dollar."""
        num = (
            input_tokens * self.input_micros_per_mtok
            + output_tokens * self.output_micros_per_mtok
        )
        return -(-num // _TOKENS_PER_MTOK)


def utc_now() -> datetime:
    return datetime.now(UTC)


class DailyBudget:
    """Persisted per-UTC-day spend against ``limit_micros`` (see module docstring)."""

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

    def today(self) -> date:
        """The current UTC day (the budget resets at 00:00 UTC)."""
        return self._clock().astimezone(UTC).date()

    def _db(self) -> sqlite3.Connection:
        # Opened lazily: a client whose budgeted target has no key never touches the DB.
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, timeout=2.0)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_daily_spend (
                    day TEXT PRIMARY KEY,
                    spent_micros INTEGER NOT NULL DEFAULT 0,
                    calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    exhausted_noted INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def spent_micros(self, day: date | None = None) -> int:
        """Micro-dollars recorded for ``day`` (default today)."""
        d = (day or self.today()).isoformat()
        row = self._db().execute(
            "SELECT spent_micros FROM llm_daily_spend WHERE day = ?", (d,)
        ).fetchone()
        return int(row[0]) if row else 0

    def remaining_micros(self) -> int:
        """Budget left today, never below 0."""
        return max(0, self.limit_micros - self.spent_micros())

    def exhausted(self) -> bool:
        """True when today's spend ≥ the limit, or the table cannot be read (fail closed)."""
        try:
            return self.spent_micros() >= self.limit_micros
        except sqlite3.Error as e:
            _log.warning("LLM budget table unreadable (%s); skipping budgeted targets", e)
            return True

    def record(self, price: ModelPrice, input_tokens: int, output_tokens: int) -> int:
        """Add one live call's cost to today's row; returns its cost in micro-dollars."""
        cost = price.cost_micros(input_tokens, output_tokens)
        try:
            conn = self._db()
            conn.execute(
                """
                INSERT INTO llm_daily_spend (day, spent_micros, calls, input_tokens, output_tokens)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    spent_micros = spent_micros + excluded.spent_micros,
                    calls = calls + 1,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens
                """,
                (self.today().isoformat(), cost, input_tokens, output_tokens),
            )
            conn.commit()
        except sqlite3.Error as e:
            _log.warning("LLM budget spend not recorded (%s)", e)
        return cost

    def note_exhausted(self) -> bool:
        """Mark today as exhausted; True only the first time per day (persisted)."""
        d = self.today().isoformat()
        try:
            conn = self._db()
            conn.execute("INSERT OR IGNORE INTO llm_daily_spend (day) VALUES (?)", (d,))
            cur = conn.execute(
                "UPDATE llm_daily_spend SET exhausted_noted = 1 "
                "WHERE day = ? AND exhausted_noted = 0",
                (d,),
            )
            conn.commit()
            return cur.rowcount == 1
        except sqlite3.Error:
            return False

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
