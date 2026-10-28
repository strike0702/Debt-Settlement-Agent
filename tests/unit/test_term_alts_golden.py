"""Term alternatives on the engine API pick what Phase 59 picked, on every recorded call.

``tests/data/term_alts_p59.json`` holds every ``find_next_term_alt`` call (inputs and
result) made by the 7 autoplay scenarios and the 100-call oracle eval (seed 7) on main
before Phase 60, when each candidate rebuilt the rules and rescanned through the
adapter's affordability cache. Phase 60 routes the same walk through
``feasibility.whatif``; each call must return the same alternative. No differences were
found, so there is no allow-list.
"""

from __future__ import annotations

import json
from datetime import date
from functools import cache
from pathlib import Path

import pytest

from app.adapter.engine_adapter import _rules_from_tuple, clear_affordability_cache
from app.agent.term_alts import find_next_term_alt
from app.domain.scenario import CallScenario, load_scenario, resolve_scenario_dir
from eval.run_eval import select_scenarios

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "term_alts_p59.json"
CALLS = json.loads(GOLDEN.read_text())["calls"]


@cache
def _eval_calls() -> dict[str, CallScenario]:
    return {s.call.id: s.call for s in select_scenarios(100, 7)}


def _scenario(source: str, sid: str) -> CallScenario:
    if source == "autoplay":
        return load_scenario(resolve_scenario_dir(sid), rebase_to=date(2026, 10, 7))
    return _eval_calls()[sid]


def test_golden_covers_the_scenarios_and_every_field() -> None:
    assert len(CALLS) == 165
    assert {c["source"] for c in CALLS} == {"autoplay", "eval"}
    fields = {c["result"][0] for c in CALLS if c["result"]}
    assert fields == {"first_payment_date", "min_payment_cents", "max_payments"}


@pytest.mark.parametrize("call", CALLS, ids=lambda c: f"{c['scenario']}")
def test_same_alternative_as_phase_59(call: dict) -> None:
    clear_affordability_cache()
    kw = dict(call["kwargs"])
    kw["fpd"] = date.fromisoformat(kw["fpd"])
    rules = _rules_from_tuple(tuple(tuple(map(tuple, v)) if isinstance(v, list) else v
                                    for v in call["rules"]))
    got = find_next_term_alt(_scenario(call["source"], call["scenario"]), rules, **kw)
    if got is not None:
        got = [got[0], got[1].isoformat() if isinstance(got[1], date) else got[1]]
    assert got == call["result"]
