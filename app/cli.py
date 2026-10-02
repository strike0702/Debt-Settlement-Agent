"""Interactive text CLI: type as the creditor rep, agent replies in the terminal.

Usage: ``python -m app.cli fixtures/demo``. Auto-acks every agent sentence (text
mode). Prints spoken lines, belief changes, guard blocks, engine verdict, and
timings. Does not open a WebSocket or use STT — that is phase 10.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.agent.orchestrator import Orchestrator, Utterance
from app.agent.policy import Intent, Phase
from app.agent.session import CallSession
from app.config import get_settings
from app.domain.scenario import load_scenario
from app.domain.units import render_money, render_pct
from app.llm.client import make_client
from app.store.audit import AuditLog


def _print_utterance(utt: Utterance) -> None:
    intent = utt.action.intent.value
    print(f"\n[agent:{intent}]")
    for _sid, text in utt.sentences:
        print(f"  {text}")
    if utt.belief_changes:
        print("[belief]")
        for ch in utt.belief_changes:
            print(
                f"  {ch.field}: {ch.old_status.value}->{ch.new_status.value} "
                f"value={ch.new_value!r}"
            )
    if utt.timings:
        t = utt.timings
        print(
            "[timing] "
            f"nlu={t.get('nlu_ms', 0):.0f}ms "
            f"policy={t.get('policy_ms', 0):.0f}ms "
            f"nlg={t.get('nlg_ms', 0):.0f}ms "
            f"total={t.get('server_total_ms', 0):.0f}ms"
        )


def _print_verdict(session: CallSession) -> None:
    aff_note = ""
    if session.last_eval is not None:
        ev = session.last_eval
        print("\n[engine verdict]")
        print(f"  feasible={ev.feasible} shape={ev.shape}")
        print(f"  offer_total={render_money(ev.offer_total_cents)}")
        if session.agreed_bp is not None:
            print(f"  settlement={render_pct(session.agreed_bp)}")
        if ev.assumed_fields:
            print(f"  assumed={ev.assumed_fields}")
        aff_note = " (max affordable is PRIVATE — not printed as a spoken figure)"
        print(f"  {aff_note.strip()}")
    if session.agreement is not None:
        agr = session.agreement
        print("\n[agreement]")
        print(f"  creditor={agr.creditor}")
        print(f"  bp={agr.bp} offer_total={agr.offer_total}")
        print(f"  status={agr.status} rows={len(agr.rows)}")


async def _run(scenario_path: Path) -> int:
    settings = get_settings()
    scenario = load_scenario(scenario_path)
    audit = AuditLog(settings.db_path)
    session = CallSession(scenario=scenario)
    llm = make_client(settings)

    orch = Orchestrator(
        session,
        llm=llm,
        settings=settings,
        audit=audit,
        auto_ack=True,
    )

    print(f"Call {session.call_id} — scenario={scenario.id} creditor={scenario.creditor}")
    print(f"Profile={settings.llm_profile} nlu={settings.nlu_mode} nlg={settings.nlg_mode}")
    print("Type as the creditor rep. Commands: /quit /barge /verdict")
    print("-" * 60)

    try:
        opening = await orch.start()
        _print_utterance(opening)

        while True:
            try:
                line = input("\nrep> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ("/quit", "/exit", "quit", "exit"):
                break
            if line == "/verdict":
                _print_verdict(session)
                continue
            if line == "/barge":
                await orch.on_barge_in([])
                print("[barge-in] pending effects dropped")
                continue

            utt = await orch.on_creditor_text(line)
            _print_utterance(utt)

            if utt.action.intent == Intent.PROPOSE_WRAP:
                _print_verdict(session)
                print(f"\n[phase] {session.neg.phase.value} — say thanks to close, or /quit.")
                continue
            if utt.action.intent in (
                Intent.CLOSE,
                Intent.NO_DEAL_WRAP,
                Intent.ESCALATE,
            ):
                _print_verdict(session)
                print(f"\n[phase] {session.neg.phase.value} — call complete.")
                break
            if session.neg.phase in (Phase.END, Phase.ESCALATE):
                print(f"\n[phase] {session.neg.phase.value} — call complete.")
                break
    finally:
        if hasattr(llm, "aclose"):
            await llm.aclose()
        audit.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Debt settlement agent text CLI")
    parser.add_argument(
        "scenario",
        nargs="?",
        default="fixtures/demo",
        help="Scenario folder with client.json, offer.json, firm.json",
    )
    args = parser.parse_args(argv)
    path = Path(args.scenario)
    if not path.is_dir():
        print(f"Scenario folder not found: {path}", file=sys.stderr)
        return 2
    return asyncio.run(_run(path))


if __name__ == "__main__":
    raise SystemExit(main())
