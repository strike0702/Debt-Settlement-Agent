"""Autoplay ("Watch a call"): the sim creditor plays the rep against the real agent.

``run_autoplay`` alternates ``sim.creditor.CreditorPolicy`` (template phrasing,
ground-truth ``TurnAnalysis`` as oracle NLU) and the normal ``Orchestrator``,
pausing between turns, and hands each creditor line and agent ``Utterance`` to
callbacks. ``app.voice.ws`` uses those callbacks to emit the usual events, so
the UI renders an autoplayed call exactly like a live one.

Autoplay never calls an LLM: ``autoplay_settings`` forces ``nlu_mode=oracle``
and template (or bank) NLG, and the orchestrator gets ``llm=None``, so it works
under ``LLM_PROFILE=offline`` with no keys. The creditor's hidden rules come
from ``fixtures/scenarios/<id>/sim.json`` (mirrors that scenario's rep card).
Phase 48: ``sim.json`` may also name how the rep haggles (``"haggle"``: a
``sim.haggle.Haggle`` style and its knobs); without it the rep is the easy
pre-46a one, so the older fixtures play exactly as before.
The policy is unchanged; ``sim/`` still never imports ``app.agent``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from app.agent.orchestrator import Orchestrator, Utterance
from app.agent.session import CallSession
from app.config import Settings
from app.domain.actions import Intent, Phase
from app.domain.scenario import CallScenario, resolve_scenario_dir
from app.schemas.events import AutoplayOutcome
from app.store.audit import AuditLog
from feasibility.models import add_months, end_of_month
from sim.creditor import CreditorPolicy
from sim.haggle import EASY, Haggle
from sim.personas import PERSONA_BY_NAME
from sim.scenarios import Scenario, TrueRules, scenario_from_truth

DEFAULT_PAUSE_MS = 1200
MAX_PAUSE_MS = 10_000
SIM_TRUTH_FILE = "sim.json"

_TERMINAL_INTENTS = (Intent.PROPOSE_WRAP, Intent.NO_DEAL_WRAP, Intent.ESCALATE)
_TERMINAL_PHASES = (Phase.WRAP, Phase.ESCALATE, Phase.END)


@dataclass(frozen=True)
class AutoplayResult:
    """How an autoplayed call ended (``outcome`` drives the ``autoplay_done`` event)."""

    outcome: AutoplayOutcome
    phase: Phase
    final_intent: Intent
    turns: int


def clamp_pause_ms(raw: Any) -> int:
    """Start-payload ``autoplay_pause_ms`` → int in ``[0, MAX_PAUSE_MS]``; default on junk."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_PAUSE_MS
    return max(0, min(MAX_PAUSE_MS, int(raw)))


def autoplay_settings(base: Settings) -> Settings:
    """Copy of ``base`` with oracle NLU and no-LLM NLG (bank if configured, else template)."""
    nlg = "bank" if base.nlg_mode == "bank" else "template"
    return base.model_copy(update={"nlu_mode": "oracle", "nlg_mode": nlg})


def _first_payment_date(spec: str, call: CallScenario) -> date:
    """``sim.json`` dates are relative so rebased demo clients stay consistent."""
    client = call.client
    if spec == "default":
        return end_of_month(client.first_draft_date)
    if spec == "after_last_draft":
        return end_of_month(add_months(client.last_draft_date, 1))
    return date.fromisoformat(spec)


_HAGGLE_STYLES = ("easy", "holder", "stepper", "staller")
_STALL_ON = ("ask", "counter")


def parse_haggle(raw: Any, *, where: str = "sim.json") -> Haggle:
    """``sim.json`` ``"haggle"`` → ``Haggle`` (absent → ``EASY``); ``ValueError`` on junk.

    Shape: ``{"style": "holder", "hold_turns": 2, "steps_bp": [1000],
    "stall_on": null}``; every key but ``style`` is optional.
    """
    if raw is None:
        return EASY
    if not isinstance(raw, dict) or raw.get("style") not in _HAGGLE_STYLES:
        raise ValueError(f"haggle in {where} needs a style, one of {', '.join(_HAGGLE_STYLES)}")
    stall_on = raw.get("stall_on")
    if stall_on is not None and stall_on not in _STALL_ON:
        raise ValueError(f"haggle.stall_on in {where} must be one of {', '.join(_STALL_ON)}")
    steps = tuple(int(x) for x in raw.get("steps_bp", (EASY.steps_bp[0],)))
    if raw["style"] != "staller" and (not steps or any(x <= 0 for x in steps)):
        raise ValueError(f"haggle.steps_bp in {where} must be positive basis points")
    return Haggle(
        style=raw["style"],
        hold_turns=int(raw.get("hold_turns", 0)),
        steps_bp=steps,
        stall_on=stall_on,
    )


def load_autoplay_scenario(scenario_id: str, call: CallScenario) -> Scenario:
    """Sim ``Scenario`` for curated ``scenario_id``; ``ValueError`` when it has no truth."""
    path = resolve_scenario_dir(scenario_id) / SIM_TRUTH_FILE
    if not path.is_file():
        raise ValueError(f"autoplay has no creditor script for scenario {scenario_id!r}")
    raw = json.loads(path.read_text())
    rules = raw["rules"]
    persona = raw.get("persona", "flexible")
    if persona not in PERSONA_BY_NAME:
        raise ValueError(f"unknown persona {persona!r} in {path}")
    truth = TrueRules(
        max_payments=int(rules["max_payments"]),
        min_payment_cents=int(rules["min_payment_cents"]),
        payment_structure=rules["payment_structure"],
        first_payment_date=_first_payment_date(str(rules["first_payment_date"]), call),
        max_segments=int(rules["max_segments"]),
        max_token_pays=int(rules["max_token_pays"]),
        min_payment_tiers=tuple(
            (int(a), int(b)) for a, b in rules.get("min_payment_tiers", [])
        ),
    )
    scenario = scenario_from_truth(
        call,
        truth,
        opening_ask_bp=int(raw["opening_ask_bp"]),
        floor_bp=int(raw["floor_bp"]),
        persona=persona,
    )
    haggle = parse_haggle(raw.get("haggle"), where=str(path))
    return scenario if haggle == EASY else replace(scenario, haggle=haggle)


def new_autoplay_call(
    call: CallScenario,
    scenario_id: str,
    *,
    settings: Settings,
    audit: AuditLog | None,
    call_id: str | None = None,
) -> tuple[Orchestrator, CreditorPolicy]:
    """Orchestrator (auto-ack, no LLM) and sim creditor for one autoplayed call."""
    sim_scenario = load_autoplay_scenario(scenario_id, call)
    session = (
        CallSession(scenario=call, call_id=call_id)
        if call_id is not None
        else CallSession(scenario=call)
    )
    orch = Orchestrator(
        session, llm=None, settings=autoplay_settings(settings), audit=audit, auto_ack=True
    )
    return orch, CreditorPolicy(sim_scenario, phrasing="template")


def outcome_of(phase: Phase, intent: Intent, *, has_agreement: bool) -> AutoplayOutcome:
    """Map the final phase / move to the demo's expected-outcome vocabulary."""
    if phase == Phase.ESCALATE or intent == Intent.ESCALATE:
        return "escalate"
    if has_agreement:
        return "deal"
    if phase == Phase.END or intent == Intent.NO_DEAL_WRAP:
        return "no_deal"
    return "incomplete"


async def run_autoplay(
    orch: Orchestrator,
    creditor: CreditorPolicy,
    *,
    on_agent: Callable[[Utterance], Awaitable[None]],
    on_creditor: Callable[[str], Awaitable[None]],
    pause_s: float = 0.0,
    max_turns: int | None = None,
) -> AutoplayResult:
    """Opening, then creditor/agent turns until a terminal move (same loop as the eval)."""
    session = orch.session
    turns = orch.settings.max_turns if max_turns is None else max_turns
    utt = await orch.start()
    await on_agent(utt)
    action = utt.action
    for _ in range(turns):
        if action.intent in _TERMINAL_INTENTS or session.neg.phase in _TERMINAL_PHASES:
            break
        if pause_s > 0:
            await asyncio.sleep(pause_s)
        last_line = utt.sentences[-1][1] if utt.sentences else ""
        reply = await creditor.respond(action, agent_text=last_line)
        await on_creditor(reply.text)
        utt = await orch.on_creditor_text(reply.text, oracle=reply.analysis)
        await on_agent(utt)
        action = utt.action
        if creditor.done:
            break
    return AutoplayResult(
        outcome=outcome_of(
            session.neg.phase, action.intent, has_agreement=session.agreement is not None
        ),
        phase=session.neg.phase,
        final_intent=action.intent,
        turns=session.neg.turn_idx,
    )
