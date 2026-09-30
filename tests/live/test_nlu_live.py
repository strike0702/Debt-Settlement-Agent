"""Live NLU accuracy against the demo LLM profile.

Skipped unless ``DSA_LIVE=1``. Fifteen synthetic rep utterances with expected
field extractions; records pass rate for PROGRESS.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from app.agent.nlu import analyze
from app.config import get_settings
from app.llm.client import LLMClient

pytestmark = pytest.mark.skipif(
    os.environ.get("DSA_LIVE") != "1",
    reason="set DSA_LIVE=1 to run live NLU tests",
)

_REF = date(2026, 4, 1)
_AGENT = "What settlement terms can you accept on this account?"


@dataclass(frozen=True)
class LiveCase:
    id: str
    utterance: str
    # field -> expected value (cents / int / enum / ISO date string)
    expect_terms: dict[str, Any]
    expect_ask_pct: float | None = None


CASES: list[LiveCase] = [
    LiveCase(
        "max_payments_six",
        "We can go as high as six payments.",
        {"max_payments": 6},
    ),
    LiveCase(
        "max_payments_twelve",
        "Maximum of twelve monthly payments.",
        {"max_payments": 12},
    ),
    LiveCase(
        "min_payment_250",
        "Minimum payment is $250.",
        {"min_payment_cents": 25000},
    ),
    LiveCase(
        "min_payment_100",
        "They need at least $100 per draft.",
        {"min_payment_cents": 10000},
    ),
    LiveCase(
        "structure_even",
        "Payments have to be even across the schedule.",
        {"payment_structure": "even"},
    ),
    LiveCase(
        "structure_balloon",
        "A balloon at the end is fine.",
        {"payment_structure": "balloon"},
    ),
    LiveCase(
        "structure_flexible",
        "Flexible payment levels are okay with us.",
        {"payment_structure": "flexible"},
    ),
    LiveCase(
        "first_payment_date",
        "First payment should be on 2026-05-15.",
        {"first_payment_date": date(2026, 5, 15)},
    ),
    LiveCase(
        "ask_45",
        "We're looking for forty-five percent of the balance.",
        {},
        expect_ask_pct=45.0,
    ),
    LiveCase(
        "ask_50",
        "Can you do 50% settlement?",
        {},
        expect_ask_pct=50.0,
    ),
    LiveCase(
        "max_segments",
        "No more than three payment tiers.",
        {"max_segments": 3},
    ),
    LiveCase(
        "token_pays",
        "Up to two token payments are allowed.",
        {"max_token_pays": 2},
    ),
    LiveCase(
        "combo_even_six",
        "Even payments, six months max, $200 minimum.",
        {
            "payment_structure": "even",
            "max_payments": 6,
            "min_payment_cents": 20000,
        },
    ),
    LiveCase(
        "hedged_min",
        "Minimum is about $300 give or take.",
        {"min_payment_cents": 30000},
    ),
    LiveCase(
        "reject_ask",
        "Forty percent is too high for us; try thirty-five.",
        {},
        expect_ask_pct=35.0,
    ),
]


def _term_map(out) -> dict[str, Any]:
    return {t.field: t.value for t in out.terms}


def _ask_close(got: float | None, expect: float | None) -> bool:
    if expect is None:
        return True
    if got is None:
        return False
    return abs(got - expect) < 0.51


@pytest.fixture
async def llm_client():
    settings = get_settings()
    settings.llm_profile = "demo"
    settings.nlu_mode = "llm"
    settings.llm_cache = False
    client = LLMClient(settings)
    try:
        yield client, settings
    finally:
        try:
            await client.aclose()
        except RuntimeError:
            pass


@pytest.mark.asyncio
async def test_nlu_live_accuracy(llm_client, capsys) -> None:
    client, settings = llm_client
    hits = 0
    details: list[str] = []
    for case in CASES:
        out = await analyze(
            case.utterance,
            _AGENT,
            None,
            llm=client,
            settings=settings,
            ref=_REF,
        )
        got = _term_map(out)
        ok = True
        for field, expect in case.expect_terms.items():
            if got.get(field) != expect:
                ok = False
        if not _ask_close(out.settlement_ask_pct, case.expect_ask_pct):
            ok = False
        if (
            case.expect_ask_pct is None
            and case.expect_terms
            and not case.expect_terms.keys() & got.keys()
        ):
            ok = False
        if ok:
            hits += 1
            details.append(f"PASS {case.id}")
        else:
            details.append(
                f"FAIL {case.id} got_terms={got} ask={out.settlement_ask_pct} "
                f"expect_terms={case.expect_terms} expect_ask={case.expect_ask_pct}"
            )

    total = len(CASES)
    accuracy = hits / total
    report = (
        f"nlu_live accuracy={hits}/{total} ({accuracy:.0%}) profile={settings.llm_profile}\n"
        + "\n".join(details)
    )
    # Always print so DSA_LIVE runs can paste into PROGRESS.
    print("\n" + report)
    # Soft floor: document whatever we got; fail only if completely broken.
    assert hits >= 1, report
