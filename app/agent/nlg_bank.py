"""Guard-validated NLG template bank (``NLG_MODE=bank``).

``config/nlg_bank.json`` is built offline by ``scripts/build_template_bank.py``:
the ``nlg`` role wrote several templates per (intent, sorted placeholder ids),
and only those passing ``template_guard`` were kept. At runtime ``pick_template``
returns one of them for an ``Action`` with no LLM call, chosen deterministically
from ``(call_id, turn)`` so a replayed call speaks the same words.

Phase 24b adds act keys: ``ACK`` (placeholders = the ``ack_*`` ids present)
and ``ANSWER:<topic>`` (no placeholders; number-free paraphrases of the
policy talking point). This module only loads and picks. ``app.agent.nlg.speak_action`` still runs
``template_guard`` (against the live action's ids) and ``rendered_guard`` on the
filled text, and falls back to ``TEMPLATES`` when no entry matches.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.agent.guards import template_guard
from app.domain.actions import Action

BankKey = tuple[str, tuple[str, ...]]

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BANK_PATH = _REPO_ROOT / "config" / "nlg_bank.json"


def bank_key(intent: str, placeholder_ids: set[str] | list[str]) -> BankKey:
    """Lookup key: intent value plus sorted placeholder ids (facts ∪ text slots)."""
    return intent, tuple(sorted(placeholder_ids))


def action_placeholder_ids(action: Action) -> set[str]:
    """Ids an action can fill: PUBLIC facts and text slots."""
    return set(action.facts) | set(action.text_slots)


def parse_bank(data: dict[str, Any]) -> dict[BankKey, list[str]]:
    """Index the JSON bank by ``bank_key``."""
    out: dict[BankKey, list[str]] = {}
    for entry in data.get("entries", []):
        key = bank_key(entry["intent"], entry["placeholders"])
        out[key] = [str(t) for t in entry["templates"]]
    return out


@lru_cache(maxsize=4)
def load_bank(path: str | Path = DEFAULT_BANK_PATH) -> dict[BankKey, list[str]]:
    """Read and index the bank; a missing file is an empty bank (TEMPLATES fallback)."""
    p = Path(path)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    if not p.exists():
        return {}
    return parse_bank(json.loads(p.read_text(encoding="utf-8")))


def pick_template(
    action: Action,
    *,
    call_id: str | None,
    turn: int,
    bank: dict[BankKey, list[str]],
    intent_key: str | None = None,
) -> str | None:
    """Deterministic bank template for ``action``, or ``None`` when nothing fits.

    Only entries that pass ``template_guard`` with this action's ids are
    candidates; the index is ``sha256(call_id:turn) mod len(candidates)``.
    ``intent_key`` overrides the bank intent (H3 acts: ``ACK``, ``ANSWER:<topic>``).
    """
    allowed = action_placeholder_ids(action)
    templates = bank.get(bank_key(intent_key or action.intent.value, allowed), [])
    ok = [t for t in templates if template_guard(t, allowed, action.required).ok]
    if not ok:
        return None
    digest = hashlib.sha256(f"{call_id or ''}:{turn}".encode()).digest()
    return ok[int.from_bytes(digest[:8], "big") % len(ok)]
