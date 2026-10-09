"""[33.2] ``scripts/build_template_bank.py`` keeps hand-reviewed bank entries.

A full rebuild (no ``--acts``) used to rewrite the whole bank from the LLM,
losing the Phase 33 hand-written READ_BACK / CLARIFY / COUNTER lines and the
act keys. Hermetic: key collection is stubbed and the ``nlg`` role is a
``FakeLLM``, so no sim run and no network.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.agent.nlg_bank import DEFAULT_BANK_PATH, bank_key, parse_bank
from app.llm.client import FakeLLM

_REVIEWED = ("READ_BACK", "CLARIFY", "COUNTER")


def _builder() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_template_bank.py"
    spec = importlib.util.spec_from_file_location("build_template_bank", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shipped() -> dict[str, Any]:
    return json.loads(DEFAULT_BANK_PATH.read_text(encoding="utf-8"))


def _rig(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replies: list[str]
) -> tuple[ModuleType, FakeLLM, Path]:
    mod = _builder()
    fake = FakeLLM()
    for r in replies:
        fake.enqueue("nlg", r)
    monkeypatch.setattr(mod, "make_client", lambda *a, **k: fake)
    out = tmp_path / "bank.json"
    out.write_text(DEFAULT_BANK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return mod, fake, out


def test_shipped_bank_marks_the_phase_33_entries_reviewed() -> None:
    reviewed = {e["intent"] for e in _shipped()["entries"] if e.get("reviewed") is True}
    assert reviewed == set(_REVIEWED)


def test_full_build_keeps_reviewed_entries_acts_and_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shipped = _shipped()
    before = parse_bank(shipped)
    refuse = bank_key("REFUSE_PRIVATE", [])
    keys = [bank_key(e["intent"], e["placeholders"]) for e in shipped["entries"]]
    keys = [k for k in keys if k[0] in _REVIEWED] + [refuse]
    # Two variants for the one unreviewed key; any extra LLM call would empty the queue.
    mod, fake, out = _rig(
        monkeypatch, tmp_path, ["I can't share that.", "That stays with the client."]
    )

    async def fake_keys(seeds: int) -> list[tuple[str, tuple[str, ...]]]:
        return sorted(keys)

    monkeypatch.setattr(mod, "collect_keys", fake_keys)
    assert mod.main(["--out", str(out), "--per-key", "2", "--profile", "offline"]) == 0

    built = json.loads(out.read_text(encoding="utf-8"))
    after = parse_bank(built)
    for k in keys[:-1]:
        assert after[k] == before[k], k  # hand copy kept, never sent to the LLM
    assert after[refuse] == ["I can't share that.", "That stays with the client."]
    assert built["stats"]["asked"] == 2
    acts = [k for k in before if k[0] == "ACK" or k[0].startswith("ANSWER:")]
    assert acts and all(after[k] == before[k] for k in acts)
    assert built["readback_reviewed"] == shipped["readback_reviewed"]
    assert built["acts_generated"] == shipped["acts_generated"]
    assert all(e.get("reviewed") for e in built["entries"] if e["intent"] in _REVIEWED)
    with pytest.raises(Exception, match="queue empty"):
        fake._pop("nlg")


def test_acts_build_skips_reviewed_act_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, fake, out = _rig(monkeypatch, tmp_path, [])
    bank = json.loads(out.read_text(encoding="utf-8"))
    for e in bank["entries"]:
        if e["intent"] == "ACK" or e["intent"].startswith("ANSWER:"):
            e["reviewed"] = True
    out.write_text(json.dumps(bank, indent=2) + "\n", encoding="utf-8")

    assert mod.main(["--out", str(out), "--acts", "--profile", "offline"]) == 0
    assert parse_bank(json.loads(out.read_text(encoding="utf-8"))) == parse_bank(bank)


def test_merge_drops_fresh_entries_for_reviewed_keys() -> None:
    mod = _builder()
    bank = {
        "entries": [
            {"intent": "READ_BACK", "placeholders": ["a"], "templates": ["hand"], "reviewed": True},
            {"intent": "COUNTER", "placeholders": ["b"], "templates": ["old"]},
            {"intent": "ACK", "placeholders": ["c"], "templates": ["ack"]},
        ]
    }
    fresh = [
        {"intent": "READ_BACK", "placeholders": ["a"], "templates": ["llm"]},
        {"intent": "COUNTER", "placeholders": ["b"], "templates": ["new"]},
    ]
    merged = parse_bank({"entries": mod.merge_entries(bank, fresh, acts=False)})
    assert merged == {
        ("READ_BACK", ("a",)): ["hand"],
        ("ACK", ("c",)): ["ack"],
        ("COUNTER", ("b",)): ["new"],
    }
