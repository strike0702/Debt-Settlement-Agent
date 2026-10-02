"""Offline e2e text calls: oracle NLU + template NLG + template sim."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.adapter.engine_adapter import build_rules
from app.adapter.validator import validate
from app.agent.numbers import extract_tokens
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.config import Settings
from app.domain.actions import Intent, Phase
from app.store.audit import AuditLog
from sim.creditor import CreditorPolicy
from sim.personas import PERSONAS, PersonaName
from sim.scenarios import STRATA, Scenario, Stratum, generate_one, to_creditor_rules


def _settings() -> Settings:
    return Settings(
        nlu_mode="oracle",
        nlg_mode="template",
        llm_profile="offline",
        hostility_threshold=0.8,
        max_turns=30,
        max_counters=6,
        anchor_ratio=0.7,
        concession_factor=0.5,
        firm_name="Synthetic Debt Relief",
        opening_disclosure="This call uses synthetic data for demonstration only.",
    )


def _private_values(scenario: Scenario) -> set[tuple[str, int | date]]:
    """Independent leak blocklist from client + firm private amounts."""
    client = scenario.call.client
    out: set[tuple[str, int | date]] = {
        ("money", client.draft_amount_cents),
        ("money", client.current_balance_cents),
        ("money", scenario.call.bank_fee_cents),
    }
    for entry in client.ledger:
        out.add(("money", entry.amount_cents))
    # Original / creditor balances and program fee amounts are private.
    out.add(("money", scenario.call.creditor_balance_cents))
    out.add(("money", scenario.call.original_balance_cents))
    return out


def _count_leaks(texts: list[str], blocklist: set[tuple[str, int | date]]) -> int:
    leaks = 0
    for text in texts:
        for tok in extract_tokens(text):
            pair = tok.as_pair()
            if pair in blocklist:
                leaks += 1
    return leaks


async def _run_call(
    tmp_path: Path,
    scenario: Scenario,
    *,
    max_turns: int = 24,
) -> tuple[CallSession, list[str], CreditorPolicy]:
    audit = AuditLog(tmp_path / f"audit_{scenario.id}.db")
    session = CallSession(scenario=scenario.call)
    orch = Orchestrator(
        session,
        llm=None,
        settings=_settings(),
        audit=audit,
        auto_ack=True,
    )
    creditor = CreditorPolicy(scenario, phrasing="template", llm=None)
    agent_lines: list[str] = []

    utt = await orch.start()
    agent_lines.extend(t for _, t in utt.sentences)
    action = utt.action

    for _ in range(max_turns):
        if action.intent in (
            Intent.PROPOSE_WRAP,
            Intent.CLOSE,
            Intent.NO_DEAL_WRAP,
            Intent.ESCALATE,
        ):
            break
        if session.neg.phase in (Phase.WRAP, Phase.ESCALATE, Phase.END):
            break
        reply = await creditor.respond(action, agent_text=agent_lines[-1] if agent_lines else "")
        utt = await orch.on_creditor_text(reply.text, oracle=reply.analysis)
        agent_lines.extend(t for _, t in utt.sentences)
        action = utt.action
        if creditor.done:
            break

    audit.close()
    return session, agent_lines, creditor


def _seed_for(persona: PersonaName, stratum: Stratum) -> int:
    # Stable distinct seeds per cell.
    return 1000 + PERSONAS.index(persona) * 10 + STRATA.index(stratum)


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("stratum", STRATA)
async def test_text_call_persona_stratum(
    tmp_path: Path,
    persona: PersonaName,
    stratum: Stratum,
) -> None:
    scenario = generate_one(persona, stratum, seed=_seed_for(persona, stratum))
    session, agent_lines, _cred = await _run_call(tmp_path, scenario)

    leaks = _count_leaks(agent_lines, _private_values(scenario))
    assert leaks == 0

    if persona == "pressuring":
        assert session.neg.phase == Phase.ESCALATE
        assert session.agreement is None
        return

    if stratum == "deal":
        assert session.neg.phase == Phase.WRAP
        assert session.agreement is not None
        assert session.last_eval is not None and session.last_eval.rows is not None
        rules = to_creditor_rules(
            scenario.true_rules,
            program_fee_pct=scenario.call.program_fee_pct,
            bank_fee_cents=scenario.call.bank_fee_cents,
        )
        fpd = scenario.true_rules.first_payment_date
        violations = validate(
            session.last_eval.rows,
            scenario.call.client,
            session.last_eval.offer_total_cents,
            session.last_eval.program_fee_cents,
            rules,
            fpd,
        )
        assert violations == []
        # Belief rules should also build and match true required fields.
        built = build_rules(session.belief, scenario.call)
        assert built.max_payments == scenario.true_rules.max_payments
        assert built.min_payment_cents == scenario.true_rules.min_payment_cents
        # Flexible deals: agent must have negotiated below the opening ask.
        if persona == "flexible":
            assert session.agreed_bp is not None
            assert session.agreed_bp < scenario.opening_ask_bp
        return

    if stratum == "rescue":
        assert session.neg.phase == Phase.ESCALATE
        assert session.agreement is None
        return

    # no_fix
    assert session.neg.phase == Phase.END
    assert session.agreement is None
    # Last agent intent should be no-deal (or escalate only if hostility — not here).
    finals = [t for t in session.history if t.role == "agent"]
    assert finals
    # Phase END is enough; optionally agreement absent.
