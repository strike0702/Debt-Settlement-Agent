"""SQLite cache for temperature-0 LLM responses (``Settings.llm_cache``, on by default).

``_cache_key`` hashes everything that can change a response (provider, model,
messages, temperature, max tokens, route params); ``_ResponseCache`` stores the
body with its token counts so a cache hit can still be audited. A rerun of the
same prompt (e.g. an NLU corpus rerun) then needs no network call.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _cache_key(
    provider: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: float,
    max_tokens: int | None,
    params: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Only add the key when set, so cache entries written before route params
    # existed (plain-string routes) still hit.
    if params:
        payload["params"] = dict(params)
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class _ResponseCache:
    """SQLite cache for temperature-0 chat responses."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (
                key TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, key: str) -> tuple[str, int | None, int | None] | None:
        row = self._conn.execute(
            "SELECT body, prompt_tokens, completion_tokens FROM llm_cache WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return row[0], row[1], row[2]

    def put(
        self,
        key: str,
        body: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO llm_cache
                (key, body, prompt_tokens, completion_tokens, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                key,
                body,
                prompt_tokens,
                completion_tokens,
                datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
