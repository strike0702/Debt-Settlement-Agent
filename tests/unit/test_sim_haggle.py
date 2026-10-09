"""Phase 46a: the haggling rep (holds, varied steps, firm floor, stallers) and plain lines."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings
from app.domain.actions import Action, Intent, Phase
from app.domain.facts import Fact
from app.domain.negotiation import accept_line_bp
from sim.creditor import _LINES, CreditorPolicy
from sim.haggle import EASY, Haggle
from sim.scenarios import Scenario, apply_haggle, generate_one
from tests.seed7 import easy_slot, slot


def _counter(bp: int) -> Action:
    fact = Fact(id="counter_pct", kind="pct", value=bp, visibility="PUBLIC", source="engine")
    return Action(
        intent=Intent.COUNTER,
        facts={"counter_pct": fact},
        required={"counter_pct"},
        next_phase=Phase.NEGOTIATE,
        reason=f"bp={bp}",
    )


def _ask() -> Action:
    return Action(intent=Intent.ASK_SETTLEMENT, next_phase=Phase.NEGOTIATE, reason="ask")


def _scenario(haggle: Haggle, *, opening: int = 7000, floor: int = 6000) -> Scenario:
    base = generate_one("flexible", "deal", seed=101)
    return replace(base, haggle=haggle, opening_ask_bp=opening, floor_bp=floor)


async def _asks(rep: CreditorPolicy, bps: list[int]) -> list[tuple[str, float | None, bool]]:
    out = []
    for bp in bps:
        r = (await rep.respond(_counter(bp))).analysis
        out.append((r.stance, r.settlement_ask_pct, r.firm))
    return out


async def test_holder_holds_then_steps_then_is_firm_at_floor() -> None:
    rep = CreditorPolicy(_scenario(Haggle("holder", hold_turns=2, steps_bp=(500, 300))))
    got = await _asks(rep, [4000] * 7)
    assert got == [
        ("reject", 70.0, False),  # hold
        ("reject", 70.0, False),  # hold
        ("reject", 65.0, False),  # step 500
        ("reject", 65.0, False),
        ("reject", 65.0, False),
        ("reject", 62.0, False),  # step 300
        ("reject", 62.0, False),
    ]
    rep2 = CreditorPolicy(_scenario(Haggle("holder", hold_turns=0, steps_bp=(900,))))
    got2 = await _asks(rep2, [4000, 4000, 5000])
    # 70 → 61 → floor 60 (firm), then restated firmly.
    assert got2 == [("reject", 61.0, False), ("reject", 60.0, True), ("reject", 60.0, True)]


async def test_haggler_accepts_only_at_or_above_floor() -> None:
    rep = CreditorPolicy(_scenario(Haggle("stepper", steps_bp=(200, 300))))
    assert (await rep.respond(_counter(5900))).analysis.stance == "reject"
    reply = await rep.respond(_counter(6000))
    assert reply.analysis.stance == "accept"
    assert "60%" in reply.text


async def test_stallers_never_name_or_take_a_number() -> None:
    ask = CreditorPolicy(_scenario(Haggle("staller", stall_on="ask", steps_bp=())))
    for _ in range(3):
        r = await ask.respond(_ask())
        assert r.analysis.stance == "other" and r.analysis.settlement_ask_pct is None
        assert not any(ch.isdigit() for ch in r.text)
    counter = CreditorPolicy(_scenario(Haggle("staller", stall_on="counter", steps_bp=())))
    assert (await counter.respond(_ask())).analysis.settlement_ask_pct == 70.0
    for bp in (4000, 6500, 9000):  # even a counter above the floor is not taken
        r = await counter.respond(_counter(bp))
        assert r.analysis.stance == "other" and r.analysis.settlement_ask_pct is None


async def test_easy_rep_is_the_pre_46a_ladder() -> None:
    rep = CreditorPolicy(_scenario(EASY, opening=7000, floor=6000))
    assert await _asks(rep, [4000, 4000, 4000]) == [
        ("reject", 65.0, False),
        ("reject", 60.0, True),
        ("reject", 60.0, True),
    ]


@pytest.mark.slow
def test_seed7_haggle_labels_stay_valid() -> None:
    """Moved deal floors sit above the agent's anchor and at or below the line."""
    anchor_ratio = Settings().anchor_ratio
    seen: set[str] = set()
    for i in range(100):
        sc, base = slot(i), easy_slot(i)
        seen.add(sc.haggle.style)
        assert sc.stratum == base.stratum and sc.zopa == base.zopa
        assert sc.should_escalate == base.should_escalate
        assert sc.true_rules == base.true_rules and sc.opening_ask_bp > sc.floor_bp
        if sc.haggle.style in ("holder", "stepper") and sc.stratum == "deal":
            assert sc.true_max_bp is not None
            line = accept_line_bp(sc.true_max_bp)
            anchor = int(anchor_ratio * min(sc.opening_ask_bp, line))
            assert anchor < sc.floor_bp <= line and sc.floor_bp in sc.feasible_bps
        if sc.haggle.style == "easy" or sc.haggle.style == "staller":
            assert (sc.floor_bp, sc.opening_ask_bp) == (base.floor_bp, base.opening_ask_bp)
        if sc.persona == "pressuring" or sc.stratum == "rescue":
            assert sc.haggle == EASY
    assert seen == {"easy", "holder", "stepper", "staller"}
    stallers = {slot(i).haggle.stall_on for i in range(100) if slot(i).haggle.style == "staller"}
    assert stallers == {"ask", "counter"}


def test_apply_haggle_is_deterministic() -> None:
    assert apply_haggle(easy_slot(0), seed=7, index=0) == slot(0)
    assert apply_haggle(easy_slot(0), seed=7, index=0) == apply_haggle(
        easy_slot(0), seed=7, index=0
    )


async def test_lines_vary_by_scenario_and_repeat_for_the_same_one() -> None:
    texts = set()
    for seed in range(12):
        sc = generate_one("flexible", "deal", seed=seed)
        a = (await CreditorPolicy(sc).respond(_ask())).text
        b = (await CreditorPolicy(sc).respond(_ask())).text
        assert a == b
        texts.add(a.replace(str(sc.opening_ask_bp // 100), "N"))
    assert len(texts) > 1


async def test_readback_answers_are_plain() -> None:
    sc = generate_one("flexible", "deal", seed=101)
    n = sc.true_rules.max_payments
    plain = set(_LINES["readback_yes"]) | {
        v.format(v=f"{n} payments") for v in _LINES["readback_yes_value"]
    }
    spoken = set()
    for sid in "abcdefgh":
        rep = CreditorPolicy(replace(sc, id=sid))
        fact = Fact(
            id="readback_value", kind="count", value=n, visibility="PUBLIC", source="creditor"
        )
        rb = Action(
            intent=Intent.READ_BACK,
            facts={"readback_value": fact},
            text_slots={"field": "max_payments"},
            next_phase=Phase.DISCOVERY,
            reason="max_payments",
        )
        reply = await rep.respond(rb)
        assert reply.analysis.readback_response == "confirm"
        # Late fields are revealed first; the answer itself closes the line.
        answer = next(t for t in plain if reply.text.endswith(t))
        spoken.add(answer)
    assert len(spoken) > 1
    assert all("not" in t or "wrong" in t for t in _LINES["readback_no"])


def test_late_field_lines_are_not_the_stiff_ones() -> None:
    stiff = {"At most {q} payment levels.", "At most {q} token payments."}
    assert not stiff & set(_LINES["max_segments"] + _LINES["max_token_pays"])
    assert all("{q}" in t for t in _LINES["max_segments"] + _LINES["max_token_pays"])
