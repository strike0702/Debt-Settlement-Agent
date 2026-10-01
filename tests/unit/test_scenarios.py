"""Unit tests for seeded scenario generation (determinism + strata)."""

from __future__ import annotations

from sim.personas import PERSONAS
from sim.scenarios import (
    STRATA,
    balanced_quota,
    generate,
    generate_one,
    stratum_counts,
)


def test_generate_deterministic_for_seed() -> None:
    a = generate(9, seed=7)
    b = generate(9, seed=7)
    assert [s.id for s in a] == [s.id for s in b]
    assert [s.stratum for s in a] == [s.stratum for s in b]
    assert [s.persona for s in a] == [s.persona for s in b]
    assert [s.opening_ask_bp for s in a] == [s.opening_ask_bp for s in b]
    assert [s.floor_bp for s in a] == [s.floor_bp for s in b]


def test_generate_hits_every_stratum() -> None:
    scenarios = generate(12, seed=3)
    counts = stratum_counts(scenarios)
    assert counts == balanced_quota(12)
    for stratum in STRATA:
        assert counts[stratum] > 0


def test_generate_different_seeds_differ() -> None:
    a = generate(6, seed=1)
    b = generate(6, seed=2)
    assert [s.id for s in a] != [s.id for s in b]


def test_generate_one_persona_stratum() -> None:
    for persona in PERSONAS:
        for stratum in STRATA:
            sc = generate_one(persona, stratum, seed=42 + hash((persona, stratum)) % 1000)
            assert sc.persona == persona
            assert sc.stratum == stratum
            assert sc.opening_ask_bp > sc.floor_bp
            if stratum == "deal":
                assert sc.zopa is True
                assert sc.true_max_bp is not None
            elif stratum == "rescue":
                assert sc.zopa is False
                assert sc.true_max_bp is None
                assert sc.rescue_within_guardrail is True
                assert sc.should_escalate is True
            else:
                assert sc.zopa is False
            if persona == "pressuring":
                assert sc.should_escalate is True
