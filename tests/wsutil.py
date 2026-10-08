"""Shared helpers for WebSocket protocol tests (offline: oracle NLU, template NLG).

``scripted_easy_deal`` plays the creditor side of ``easy_deal`` by hand (rules,
45% ask, accept), acking every ``say``, and returns every frame received.
``leaked_private_values`` is the rep-stream privacy scan used by the F7 tests.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.agent.numbers import extract_tokens
from app.config import Settings
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog


def offline_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "nlu_mode": "oracle",
        "nlg_mode": "template",
        "llm_profile": "offline",
        "hostility_threshold": 0.8,
        "max_turns": 24,
        "max_counters": 4,
        "anchor_ratio": 0.7,
        "concession_factor": 0.5,
        "firm_name": "Synthetic Debt Relief",
        "opening_disclosure": "This call uses synthetic data for demonstration only.",
        "db_path": ":memory:",
    }
    base.update(overrides)
    return Settings(**base)


def make_client(tmp_path: Path, *, settings: Settings | None = None, llm: Any = None) -> TestClient:
    audit = AuditLog(tmp_path / "ws_audit.db")
    app = create_app(settings=settings or offline_settings(), llm=llm or FakeLLM(), audit=audit)
    return TestClient(app)


def recv_until(ws: Any, predicate: Callable[[dict], bool], *, limit: int = 400) -> list[dict]:
    got: list[dict] = []
    for _ in range(limit):
        msg = ws.receive_json()
        got.append(msg)
        if predicate(msg):
            return got
    raise AssertionError(f"predicate not met in {limit} frames: {[m.get('type') for m in got]}")


def turn(ws: Any) -> list[dict]:
    return recv_until(ws, lambda m: m.get("type") == "turn_done")


def ack_all(ws: Any, events: list[dict]) -> list[dict]:
    """Ack every ``say`` in ``events``; return the frames the acks produced."""
    out: list[dict] = []
    for ev in events:
        if ev.get("type") == "say":
            ws.send_json({"type": "sentence_done", "id": ev["id"]})
            out.extend(turn(ws))
    return out


_RULES = {
    "type": "text",
    "text": "Max eight payments, minimum one hundred dollars, even payments please.",
    "oracle": {
        "terms": [
            {"field": "max_payments", "value": 8, "quote": "eight", "hedged": False},
            {
                "field": "min_payment_cents",
                "value": 10000,
                "quote": "one hundred dollars",
                "hedged": False,
            },
            {"field": "payment_structure", "value": "even", "quote": "even", "hedged": False},
        ],
        "stance": "info",
    },
}
_ASK = {
    "type": "text",
    "text": "We are looking for a forty five percent settlement.",
    "oracle": {"settlement_ask_pct": 45.0, "ask_quote": "forty five percent", "stance": "offer"},
}
_ACCEPT = {"type": "text", "text": "Agreed.", "oracle": {"stance": "accept"}}


def scripted_easy_deal(ws: Any) -> list[dict]:
    """Start ``easy_deal`` and play rules → 45% ask → accept until WRAP; all frames."""
    frames: list[dict] = []
    ws.send_json({"type": "start", "scenario_id": "easy_deal"})
    batch = turn(ws)
    frames += batch + ack_all(ws, batch)
    for msg in (_RULES, _ASK, _ACCEPT, _ACCEPT):
        ws.send_json(msg)
        batch = turn(ws)
        frames += batch + ack_all(ws, batch)
        if any(m.get("type") == "agreement" for m in frames):
            break
    return frames


def _walk(value: Any, key: str = "", path: str = "") -> Iterable[tuple[str, str, Any]]:
    """``(path, key, leaf)``; ``key`` is the nearest dict key (list items inherit it)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, k, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, key, f"{path}[{i}]")
    else:
        yield path, key, value


def _is_timestamp(key: str, leaf: str) -> bool:
    """An ISO datetime under ``ts``: its ``SS.ffffff`` tokenizes as money (``09.50`` = 950
    cents) and collided with a private fee once (36.1). Only real datetimes are skipped."""
    if key != "ts" or "T" not in leaf:
        return False
    try:
        datetime.fromisoformat(leaf)
    except ValueError:
        return False
    return True


# Ints under these keys are counters / ids / timings, not money or percentages.
_NON_AMOUNT_KEYS = ("turn", "turns", "id", "n_feasible", "attempt")
_MONEY_KEYS = ("offer_total", "amount")


def _int_kinds(key: str) -> tuple[str, ...]:
    """Which private kinds an int under ``key`` can be (unknown keys: both)."""
    if key == "bp" or key.endswith("_bp"):
        return ("pct",)
    if key.endswith("_cents") or key in _MONEY_KEYS:
        return ("money",)
    return ("money", "pct")


def leaked_private_values(
    frames: list[dict],
    private: set[tuple[str, int | date]],
    *,
    ref: date,
) -> list[tuple[str, Any]]:
    """Private (kind, value) pairs found in ``frames``.

    Exempt, as in the eval leak scan: a private value the agent itself spoke
    (it passed ``rendered_guard`` as a PUBLIC fact, e.g. a counter equal to the
    ceiling), and, under keys with no unit, a number the rep said (their own
    ``$100`` minimum is 10000 cents, which equals a 100% ``max_bp``). Ints are
    matched by the unit their key implies; strings are tokenized with
    ``extract_tokens``; ISO date strings are matched as dates. ISO datetimes
    under ``ts`` are skipped (audit clock, not an amount; see ``_is_timestamp``).
    Each hit is ``("<frame type>:<key path>", value)``.
    """
    spoken: set[tuple[str, int | date]] = set()
    said: set[int | date] = set()
    for f in frames:
        if f.get("type") == "say":
            spoken |= {t.as_pair() for t in extract_tokens(f["text"], ref=ref)}
        if f.get("type") == "transcript" and f.get("role") == "creditor":
            said |= {t.value for t in extract_tokens(f["text"], ref=ref)}
    targets = private - spoken
    by_kind = {
        kind: {v for k, v in targets if k == kind and isinstance(v, int) and v >= 100}
        for kind in ("money", "pct")
    }
    dates = {v for k, v in targets if k == "date"}
    hits: list[tuple[str, Any]] = []
    for f in frames:
        for path, key, leaf in _walk(f):
            if isinstance(leaf, bool) or key.endswith("_ms") or key in _NON_AMOUNT_KEYS:
                continue
            if isinstance(leaf, str) and _is_timestamp(key, leaf):
                continue
            where = f"{f.get('type')}:{path}"
            if isinstance(leaf, int):
                kinds = _int_kinds(key)
                if len(kinds) > 1 and leaf in said:
                    continue
                if any(leaf in by_kind[k] for k in kinds):
                    hits.append((where, leaf))
            elif isinstance(leaf, str):
                try:
                    if date.fromisoformat(leaf) in dates:
                        hits.append((where, leaf))
                    continue
                except ValueError:
                    pass
                for tok in extract_tokens(leaf, ref=ref):
                    if tok.as_pair() in targets:
                        hits.append((where, tok.raw))
    return hits
