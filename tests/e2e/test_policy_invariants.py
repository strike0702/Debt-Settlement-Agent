"""Policy invariants over the offline oracle eval (oracle NLU, template NLG + sim).

The sweep runs one scenario per seed, cycling stratum × persona.
``DSA_INVARIANT_SEEDS`` sets the seed count (default 100; acceptance uses 500).
"""

from __future__ import annotations

import os
from pathlib import Path
from random import Random

import pytest

from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.domain.actions import Intent, Phase
from app.domain.negotiation import accept_line_bp
from app.llm.client import make_client
from app.store.audit import AuditLog
from eval.run_eval import _agreement_valid, _build_settings, run_one_scenario
from sim.creditor import CreditorPolicy
from sim.personas import PERSONAS
from sim.scenarios import STRATA, Scenario, generate, generate_one
from tests.seed7 import easy_slot as seed7_easy_slot
from tests.seed7 import slot as seed7_slot
from tests.seed7 import tiered as seed7_tiered

_SETTINGS = _build_settings(profile="offline", nlg="template", nlu="oracle")
_TERMINAL = (Intent.PROPOSE_WRAP, Intent.NO_DEAL_WRAP, Intent.ESCALATE)
_N_SEEDS = int(os.environ.get("DSA_INVARIANT_SEEDS", "100"))


async def test_regression_s0007_009_no_fix_flexible_counter_loop(tmp_path: Path) -> None:
    """Phase 12 eval (seed 7, n=12) spoke 10 COUNTERs, last four identical at 62%."""
    sc = next(s for s in generate(12, 7) if s.id == "s0007_009_no_fix_flexible")
    r = await run_one_scenario(
        sc,
        settings=_SETTINGS,
        llm=make_client(_SETTINGS),
        sim_phrasing="template",
        audit_dir=tmp_path,
    )
    assert r["status"] == "ok"
    assert r["counters_spoken"] <= _SETTINGS.max_counters
    assert r["identical_consecutive_agent_moves"] == 0
    # Deal-or-handoff (Phase 45): a no_fix call ends in a handoff, never a no-deal end.
    assert r["final_intent"] == Intent.ESCALATE.value
    assert r["final_reason"] in ("above_accept_line", "max_counters", "no_legal_counter")


async def test_regression_s0007_000_tiers_readback_after_accept(tmp_path: Path) -> None:
    """Seed-7 n=100: 'Agreed' at 48%, tiers READ_BACK, then a 58% COUNTER + re-CONFIRM."""
    # Pre-46a easy rep (slot 0 is a holder since Phase 46a; this pins the old path).
    sc = seed7_easy_slot(0)
    assert sc.id == "s0007_000_deal_flexible"
    r = await run_one_scenario(
        sc,
        settings=_SETTINGS,
        llm=make_client(_SETTINGS),
        sim_phrasing="template",
        audit_dir=tmp_path,
    )
    assert r["intents"][-3:] == ["CONFIRM_SCHEDULE", "READ_BACK", "PROPOSE_WRAP"]
    assert r["intents"].count("CONFIRM_SCHEDULE") == 1
    assert r["agreed_bp"] == 4800
    assert not any("[]" in line for line in r["agent_lines"])


async def _run_call(sc: Scenario, audit_dir: Path) -> list[str]:
    """Drive one call and return invariant violations (empty = pass)."""
    session = CallSession(scenario=sc.call)
    audit = AuditLog(audit_dir / f"{sc.id}.db")
    orch = Orchestrator(
        session, llm=make_client(_SETTINGS), settings=_SETTINGS, audit=audit, auto_ack=True
    )
    creditor = CreditorPolicy(sc, phrasing="template")
    bad: list[str] = []
    counters: list[int] = []
    prev_counter: int | None = None
    action = (await orch.start()).action
    try:
        for _ in range(_SETTINGS.max_turns + 1):
            if action.intent in _TERMINAL or session.neg.phase in (
                Phase.WRAP,
                Phase.ESCALATE,
                Phase.END,
            ):
                break
            reply = await creditor.respond(action)
            action = (await orch.on_creditor_text(reply.text, oracle=reply.analysis)).action
            max_bp = session.last_max_bp
            line = None if max_bp is None else accept_line_bp(max_bp)
            if action.intent == Intent.NO_DEAL_WRAP:
                bad.append(f"no-deal end ({action.reason}); calls end in a deal or a handoff")
            if action.intent == Intent.CONFIRM_SCHEDULE:
                bp = int(action.facts["settlement_pct"].value)  # type: ignore[arg-type]
                if line is None or bp > line:
                    bad.append(f"confirm {bp} > accept line {line}")
            if action.intent != Intent.COUNTER:
                prev_counter = None
                continue
            bp = int(action.facts["counter_pct"].value)  # type: ignore[arg-type]
            ask = session.neg.ask_bp
            if ask is not None and bp >= ask:
                bad.append(f"counter {bp} >= ask {ask}")
            if line is None or bp > line:
                bad.append(f"counter {bp} > accept line {line}")
            # A hold restates our last offer on purpose (Phase 45); nothing else may.
            if prev_counter == bp and action.reason != "hold":
                bad.append(f"identical consecutive counter {bp}")
            counters.append(bp)
            prev_counter = bp
        else:
            bad.append("did not terminate within max_turns")
    finally:
        audit.close()
    if len(counters) > _SETTINGS.max_counters:
        bad.append(f"{len(counters)} counters > {_SETTINGS.max_counters}")
    if session.neg.phase not in (Phase.WRAP, Phase.ESCALATE) and session.agreement is None:
        bad.append(f"ended in {session.neg.phase.value} without a deal or a handoff")
    if session.neg.phase == Phase.WRAP and not _agreement_valid(
        sc, session, creditor.agreed_rules
    ):
        bad.append("WRAP agreement invalid under agreed true rules")
    return bad


def _seed_scenario(seed: int) -> Scenario:
    """Sub-seed from ``Random(seed)`` like ``generate``; redraw if sampling fails."""
    stratum = STRATA[seed % len(STRATA)]
    persona = PERSONAS[(seed // len(STRATA)) % len(PERSONAS)]
    rng = Random(seed)
    for _ in range(10):
        try:
            return generate_one(persona, stratum, rng.randint(0, 2**31 - 1))
        except RuntimeError:
            continue
    raise RuntimeError(f"no {stratum}/{persona} scenario for seed {seed}")


async def test_ladder_through_the_sim_settles_at_or_below_the_line(tmp_path: Path) -> None:
    """Phase 45 ladder against the real sim: floor raised to the accept line, so the
    rep concedes, reaches its floor, says it is firm, and the call ends in a deal at
    or below the line within ``max_counters`` counters (no first-number accept)."""
    import dataclasses

    base = seed7_easy_slot(0)
    assert base.true_max_bp is not None
    line = accept_line_bp(base.true_max_bp)
    floor = max(bp for bp in base.feasible_bps if bp <= line)
    sc = dataclasses.replace(base, floor_bp=floor, opening_ask_bp=min(9500, floor + 2000))
    r = await run_one_scenario(
        sc, settings=_SETTINGS, llm=make_client(_SETTINGS), sim_phrasing="template",
        audit_dir=tmp_path,
    )
    assert r["got_deal"], r["intents"]
    assert r["agreed_bp"] <= line
    assert 1 <= r["counters_spoken"] <= _SETTINGS.max_counters
    assert r["intents"].index("COUNTER") < r["intents"].index("CONFIRM_SCHEDULE")
    # Floor just above the line: the firm rep is handed off, never accepted.
    above = dataclasses.replace(sc, floor_bp=floor + 200, opening_ask_bp=floor + 2200)
    r2 = await run_one_scenario(
        above, settings=_SETTINGS, llm=make_client(_SETTINGS), sim_phrasing="template",
        audit_dir=tmp_path,
    )
    assert not r2["got_deal"] and r2["escalated"]
    assert r2["final_reason"] in ("above_accept_line", "max_counters")


@pytest.mark.slow
async def test_policy_invariants_over_seeds(tmp_path: Path) -> None:
    failures: dict[str, list[str]] = {}
    for seed in range(_N_SEEDS):
        sc = _seed_scenario(seed)
        bad = await _run_call(sc, tmp_path)
        if bad:
            failures[sc.id] = bad
    assert not failures, failures


@pytest.mark.slow
def test_seed7_slots_match_generate() -> None:
    """``tests.seed7`` (used by fast tests) must equal the real ``generate(100, 7)``."""
    full = generate(100, 7)
    for i in (0, 2, 10, 50, 78, 99):
        assert seed7_slot(i) == full[i]
    assert seed7_tiered() == tuple(s for s in full if s.true_rules.min_payment_tiers)
