"""A/B arms (Phase 24a): policy adapter parity, ReAct / LLM-only with FakeLLM.

Everything runs offline: oracle NLU, template sim, and a ``FakeLLM`` subclass
whose queued replies may be callables of the prompt (so a script can read the
creditor's ask and the ceiling from the context block).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.session import CallSession
from app.domain.actions import Intent, Phase
from app.domain.belief import TermStatus
from app.llm.client import FakeLLM
from eval.agents import AGENT_NAMES, PolicyAgent, make_agent
from eval.agents.arm_metrics import arm_metrics
from eval.agents.base import AGENT_ROLE, MOVE_DOCS, Move, MoveError, coerce_bp, parse_json_object
from eval.agents.llm_only_agent import LLMOnlyAgent
from eval.agents.react_agent import MAX_STEPS, ReactAgent
from eval.run_eval import _build_settings, main, run_one_scenario
from sim.creditor import CreditorPolicy
from tests.seed7 import slot

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "policy_arm_golden.json"
# Keys added by Phase 24a; everything else must match the pre-24a runner.
_NEW_KEYS = {"agent", "transcript", "llm_calls_per_turn", "turn_latency_ms", "timings"}


class ScriptedLLM(FakeLLM):
    """FakeLLM whose queued replies may be ``callable(messages) -> str``."""

    async def chat_text(self, role: Any, messages: Any, max_tokens: int) -> str:
        raw = await self._take(role)
        return raw(messages) if callable(raw) else str(raw)


# The golden was recorded with .env.example's disclosure, not the Settings default;
# pin it so the test does not depend on a local .env (CI has none).
_GOLDEN_DISCLOSURE = (
    "You are speaking with an automated agent authorized to discuss settlement options."
)


def _oracle_settings() -> Any:
    s = _build_settings(profile="offline", nlg="template", nlu="oracle")
    return s.model_copy(update={"opening_disclosure": _GOLDEN_DISCLOSURE})


def _bp_in_prompt(pattern: str, messages: list[dict[str, Any]]) -> int:
    m = re.search(pattern, messages[-1]["content"])
    assert m is not None, pattern
    return int(m.group(1))


def _confirm_at_ask(messages: list[dict[str, Any]]) -> str:
    ask = _bp_in_prompt(r"ask: \S+ \(bp=(\d+)\)", messages)
    ceiling = _bp_in_prompt(r"max_bp=(\d+)", messages)
    return json.dumps(
        {"tool": "confirm_schedule", "args": {"bp": min(ask, ceiling)}, "text": "Shall we confirm?"}
    )


def _evaluate_at_ask(messages: list[dict[str, Any]]) -> str:
    ask = _bp_in_prompt(r"ask: \S+ \(bp=(\d+)\)", messages)
    return json.dumps({"tool": "evaluate_offer", "args": {"bp": ask}})


def _script(llm: FakeLLM, replies: list[str | Callable[..., str]]) -> None:
    for r in replies:
        llm.enqueue(AGENT_ROLE, r)


async def _run(agent: str, llm: FakeLLM, i: int = 0, tmp: Path | None = None) -> dict[str, Any]:
    assert tmp is not None
    return await run_one_scenario(
        slot(i),
        settings=_oracle_settings(),
        llm=llm,
        sim_phrasing="template",
        audit_dir=tmp,
        agent=agent,
    )


# ----- policy arm: CI path unchanged -----


async def test_policy_agent_output_identical_to_pre_24a_runner(tmp_path: Path) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    settings = _oracle_settings()
    llm = FakeLLM()
    for sid, expected in golden.items():
        i = int(sid.split("_")[1])
        got = await run_one_scenario(
            slot(i), settings=settings, llm=llm, sim_phrasing="template", audit_dir=tmp_path
        )
        assert got["agent"] == "policy"
        assert all(c == 0 for c in got["llm_calls_per_turn"])
        assert len(got["turn_latency_ms"]) == len(got["intents"])
        old = {k: v for k, v in got.items() if k not in _NEW_KEYS}
        assert json.loads(json.dumps(old, sort_keys=True, default=str)) == expected, sid


def test_make_agent_names_and_policy_adapter() -> None:
    assert AGENT_NAMES == ("policy", "policy_h3", "react", "llm_only")
    session = CallSession(scenario=slot(0).call)
    agent = make_agent("policy", session, llm=None, settings=_oracle_settings(), audit=None)
    assert isinstance(agent, PolicyAgent)
    assert agent.orchestrator.session is session
    h3 = make_agent("policy_h3", session, llm=None, settings=_oracle_settings(), audit=None)
    assert isinstance(h3, PolicyAgent) and h3.name == "policy_h3"
    assert h3.orchestrator.settings.nlg_h3 is True
    assert agent.orchestrator.settings.nlg_h3 is False
    with pytest.raises(ValueError):
        make_agent("nope", session, llm=None, settings=_oracle_settings(), audit=None)
    with pytest.raises(ValueError):
        make_agent("react", session, llm=None, settings=_oracle_settings(), audit=None)


def test_cli_agent_flag_defaults_to_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_main(args: Any) -> int:
        seen["agent"] = args.agent
        return 0

    monkeypatch.setattr("eval.run_eval._async_main", fake_main)
    assert main(["--nlu", "oracle", "--nlg", "template", "--sim-phrasing", "template"]) == 0
    assert seen["agent"] == "policy"
    assert main(["--agent", "react"]) == 0
    assert seen["agent"] == "react"
    with pytest.raises(SystemExit):
        main(["--agent", "bogus"])


# ----- ReAct arm -----


async def test_react_runs_offline_scenario_end_to_end(tmp_path: Path) -> None:
    llm = ScriptedLLM()
    _script(
        llm,
        [
            # Turn 1 (rules revealed): observe, then ask for the settlement.
            json.dumps({"tool": "get_rules", "thought": "check what I know"}),
            json.dumps({"tool": "ask_settlement", "text": "What settlement do you need?"}),
            # Turn 2 (ask stated): check the engine, then confirm at the ask.
            _evaluate_at_ask,
            _confirm_at_ask,
            # Turn 3 (accepted): wrap.
            json.dumps({"tool": "propose_wrap", "text": "Thank you, I will send it over."}),
        ],
    )
    r = await _run("react", llm, 0, tmp_path)
    assert r["status"] == "ok", r.get("error")
    assert r["agent"] == "react"
    assert r["intents"] == ["OPENING", "ASK_SETTLEMENT", "CONFIRM_SCHEDULE", "PROPOSE_WRAP"]
    assert r["phase"] == Phase.WRAP.value
    assert r["got_deal"] is True
    assert r["agreement_valid"] is True
    assert r["llm_calls_per_turn"] == [0, 2, 2, 1]
    assert r["transcript"][2] == {"role": "agent", "text": "What settlement do you need?"}
    assert r["sensitive_leaks"] == 0
    assert len(r["turn_latency_ms"]) == 4


async def test_react_step_cap_falls_back_after_four_calls(tmp_path: Path) -> None:
    llm = ScriptedLLM()
    # Observations on every step: the last step refuses them, then the cap hits.
    _script(llm, [json.dumps({"tool": "get_rules"})] * MAX_STEPS)
    session = CallSession(scenario=slot(0).call)
    agent = ReactAgent(session, llm=llm, settings=_oracle_settings(), audit=None)
    reply = await CreditorPolicy(slot(0)).respond((await agent.start()).action)
    utt = await agent.on_creditor_text(reply.text, oracle=reply.analysis)
    assert agent.last_turn_llm_calls == MAX_STEPS
    assert utt.action.intent == Intent.ASK_SETTLEMENT
    assert utt.action.reason == "step_cap"


async def test_react_guard_rejects_infeasible_counter_then_accepts_next(tmp_path: Path) -> None:
    llm = ScriptedLLM()
    session = CallSession(scenario=slot(0).call)
    agent = ReactAgent(session, llm=llm, settings=_oracle_settings(), audit=None)
    creditor = CreditorPolicy(slot(0))
    reply = await creditor.respond((await agent.start()).action)
    _script(
        llm,
        [
            "not json at all",
            # Slot 0: 2% fails the payment floors, so the engine guard rejects it.
            json.dumps({"tool": "propose_counter", "args": {"bp": 200}, "text": "2%?"}),
            json.dumps({"tool": "propose_counter", "args": {"bp": 3000}, "text": "How about 30%?"}),
        ],
    )
    utt = await agent.on_creditor_text(reply.text, oracle=reply.analysis)
    assert agent.last_turn_llm_calls == 3
    assert utt.action.intent == Intent.COUNTER
    assert utt.action.facts["counter_pct"].value == 3000
    assert "offer_total" in utt.action.facts
    assert session.neg.counters_offered == [3000]


# ----- tool → Action mapping -----


async def _ready_agent(cls: type = ReactAgent) -> Any:
    """Agent whose belief holds the revealed core rules (engine usable)."""
    llm = ScriptedLLM()
    session = CallSession(scenario=slot(0).call)
    agent = cls(session, llm=llm, settings=_oracle_settings(), audit=None)
    reply = await CreditorPolicy(slot(0)).respond((await agent.start()).action)
    llm.enqueue(AGENT_ROLE, json.dumps({"tool": "say", "args": {"text": "Thanks."}}))
    llm.enqueue(AGENT_ROLE, json.dumps({"text": "Thanks.", "move": {"tool": "say"}}))
    await agent.on_creditor_text(reply.text, oracle=reply.analysis)
    return agent


async def test_tool_to_action_mapping() -> None:
    agent = await _ready_agent()
    assert agent.afford is not None and agent.afford.max_bp is not None
    ok_bp = agent.afford.feasible_bps[0]
    bad_bp = next(b for b in range(200, 10001, 100) if b not in agent.afford.feasible_bps)

    async def act(tool: str, **args: Any) -> Any:
        return await agent.build_action(Move(tool, args))

    assert (await act("say")).intent == Intent.ASK_SETTLEMENT
    assert (await act("ask_settlement")).intent == Intent.ASK_SETTLEMENT
    ask = await act("ask", field="max_segments")
    assert (ask.intent, ask.text_slots["field"], ask.reason) == (
        Intent.ASK,
        "max_segments",
        "max_segments",
    )
    rb = await act("read_back", field="max_payments")
    assert rb.intent == Intent.READ_BACK
    assert rb.facts["readback_value"].kind == "count"
    rb_enum = await act("read_back", field="payment_structure")
    assert rb_enum.text_slots["readback_value"] in ("even", "balloon", "flexible")
    assert (await act("clarify", field="max_payments")).intent == Intent.CLARIFY
    counter = await act("propose_counter", bp=ok_bp)
    assert counter.intent == Intent.COUNTER
    assert counter.facts["counter_pct"].value == ok_bp
    assert all(f.visibility == "PUBLIC" for f in counter.facts.values())
    confirm = await act("confirm_schedule", bp=ok_bp)
    assert confirm.intent == Intent.CONFIRM_SCHEDULE
    assert {"settlement_pct", "offer_total", "num_payments"} <= set(confirm.facts)
    assert all(f.visibility == "PUBLIC" for f in confirm.facts.values())
    terms = await act("propose_terms", field="first_payment_date", value="2026-05-31")
    assert terms.intent == Intent.COUNTER_TERMS
    assert terms.facts["alt_first_payment_date"].value == date(2026, 5, 31)
    terms_min = await act("propose_terms", field="min_payment_cents", value="$90")
    assert terms_min.facts["alt_min_payment_cents"].value == 9000
    assert (await act("refuse_private")).intent == Intent.REFUSE_PRIVATE
    assert (await act("refuse_commit")).intent == Intent.REFUSE_COMMIT
    esc = await act("escalate", reason="hostile")
    assert (esc.intent, esc.next_phase) == (Intent.ESCALATE, Phase.ESCALATE)
    nd = await act("end_no_deal", reason="unaffordable")
    assert (nd.intent, nd.next_phase) == (Intent.NO_DEAL_WRAP, Phase.END)
    # Guarded: no wrap before a confirmed schedule; unknown field / tool rejected.
    with pytest.raises(MoveError):
        await act("propose_wrap")
    with pytest.raises(MoveError):
        await act("ask", field="income")
    with pytest.raises(MoveError):
        await act("transfer_funds")
    with pytest.raises(MoveError):
        await act("propose_counter", bp=bad_bp)
    with pytest.raises(MoveError):
        await act("confirm_schedule", bp=bad_bp)


async def test_unguarded_llm_only_lets_bad_moves_through() -> None:
    agent = await _ready_agent(LLMOnlyAgent)
    assert agent.guarded is False
    counter = await agent.build_action(Move("propose_counter", {"bp": 200}))
    assert counter.facts["counter_pct"].value == 200
    assert "offer_total" not in counter.facts
    wrap = await agent.build_action(Move("propose_wrap", {}))
    assert wrap.intent == Intent.PROPOSE_WRAP


def test_coerce_bp_and_json_parsing() -> None:
    assert coerce_bp(4500) == 4500
    assert coerce_bp("45%") == 4500
    assert coerce_bp(45) == 4500
    assert coerce_bp("4500") == 4500
    for bad in (None, True, "lots", 0, 20000):
        with pytest.raises(MoveError):
            coerce_bp(bad)
    assert parse_json_object('```json\n{"tool": "say"}\n```') == {"tool": "say"}
    assert parse_json_object('Sure! {"tool": "say"} done') == {"tool": "say"}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


# ----- LLM-only arm -----


async def test_llm_only_one_call_per_turn_end_to_end(tmp_path: Path) -> None:
    llm = ScriptedLLM()

    def confirm(messages: list[dict[str, Any]]) -> str:
        bp = json.loads(_confirm_at_ask(messages))["args"]["bp"]
        move = {"tool": "confirm_schedule", "args": {"bp": bp}}
        return json.dumps({"text": "Can we confirm?", "move": move})

    _script(
        llm,
        [
            json.dumps({"text": "What do you need?", "move": {"tool": "ask_settlement"}}),
            confirm,
            json.dumps({"text": "Thank you.", "move": {"tool": "propose_wrap"}}),
        ],
    )
    r = await _run("llm_only", llm, 0, tmp_path)
    assert r["status"] == "ok", r.get("error")
    assert r["intents"] == ["OPENING", "ASK_SETTLEMENT", "CONFIRM_SCHEDULE", "PROPOSE_WRAP"]
    assert r["llm_calls_per_turn"] == [0, 1, 1, 1]
    assert r["agreement_valid"] is True


async def test_llm_only_bad_reply_falls_back_without_retry() -> None:
    llm = ScriptedLLM()
    session = CallSession(scenario=slot(0).call)
    agent = LLMOnlyAgent(session, llm=llm, settings=_oracle_settings(), audit=None)
    reply = await CreditorPolicy(slot(0)).respond((await agent.start()).action)
    llm.enqueue(AGENT_ROLE, "I think we should counter at 40%")
    utt = await agent.on_creditor_text(reply.text, oracle=reply.analysis)
    assert agent.last_turn_llm_calls == 1
    assert utt.action.reason == "invalid_reply"
    assert not any(ch.isdigit() for _, t in utt.sentences for ch in t)


async def test_llm_arm_wrap_without_schedule_scores_invalid(tmp_path: Path) -> None:
    llm = ScriptedLLM()
    _script(llm, [json.dumps({"text": "We have a deal.", "move": {"tool": "propose_wrap"}})])
    r = await _run("llm_only", llm, 0, tmp_path)
    assert r["phase"] == Phase.WRAP.value
    assert r["got_deal"] is False
    assert r["agreement_valid"] is False


async def test_llm_arm_leak_of_ceiling_is_counted(tmp_path: Path) -> None:
    llm = ScriptedLLM()

    def leak(messages: list[dict[str, Any]]) -> str:
        ceiling = _bp_in_prompt(r"max_bp=(\d+)", messages)
        pct = f"{ceiling // 100}%"
        return json.dumps({"text": f"Our maximum is {pct}.", "move": {"tool": "say"}})

    _script(llm, [leak, json.dumps({"text": "Bye.", "move": {"tool": "end_no_deal"}})])
    r = await _run("llm_only", llm, 0, tmp_path)
    assert r["sensitive_leaks"] >= 1


async def test_shared_perception_fills_belief() -> None:
    agent = await _ready_agent()
    belief = agent.session.belief
    for f in ("max_payments", "min_payment_cents", "payment_structure", "first_payment_date"):
        assert belief.get(f).status == TermStatus.KNOWN
    ctx = agent.context_block()
    assert "PRIVATE" in ctx and "max_bp=" in ctx and "REP:" in ctx


def test_arm_metrics_summary() -> None:
    out = arm_metrics(
        [
            {"status": "ok", "llm_calls_per_turn": [0, 2, 4], "turn_latency_ms": [1.0, 3.0, None]},
            {"status": "skipped_quota"},
        ]
    )
    assert out["turns"] == 3
    assert out["llm_calls_per_turn_mean"] == 2.0
    assert out["llm_calls_per_turn_max"] == 4.0
    assert out["turn_latency_ms_p50"] == 2.0


def test_agent_steps_use_dedicated_agent_role() -> None:
    """Carry-over 24a.1: agent calls no longer share the NLU role / timeout."""
    assert AGENT_ROLE == "agent"


def test_coerce_bp_percent_form_is_prompted_and_unambiguous() -> None:
    """Carry-over 24a.4: prompts ask for "N%"; that form is exact even for 1%."""
    assert '"45%"' in MOVE_DOCS
    assert coerce_bp("1%") == 100
    assert coerce_bp("45%") == 4500
    assert coerce_bp("45.5%") == 4550
    assert coerce_bp(4500) == 4500
    # Documented heuristic: a bare number ≤ 100 is percent, so bare 1 is 1%, not 1 bp.
    assert coerce_bp(1) == 100


def test_build_settings_keeps_explicit_key_pool() -> None:
    """Carry-over 27.2: a pool set on ``base`` survives the eval settings rebuild."""
    from pydantic import SecretStr

    from app.config import Settings
    from eval.run_eval import _build_settings

    base = Settings(api_key_pool={"GROQ_API_KEY_1": SecretStr("sk-one")}, llm_key_cooldown_s=7.0)
    built = _build_settings(profile="eval", nlg="bank", base=base)
    assert built.api_keys("GROQ_API_KEY")[-1] == (1, "sk-one")
    assert built.llm_key_cooldown_s == 7.0 and built.nlg_mode == "bank"


def test_cli_policy_h3_bank_and_providers_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_main(args: Any) -> int:
        seen.update(vars(args))
        return 0

    monkeypatch.setattr("eval.run_eval._async_main", fake_main)
    argv = ["--agent", "policy_h3", "--nlg", "bank", "--providers", "x.yaml"]
    assert main(argv) == 0
    assert (seen["agent"], seen["nlg"], seen["providers"]) == ("policy_h3", "bank", "x.yaml")
    # Bank NLG needs no LLM, so the offline oracle layer accepts it.
    assert main(["--nlu", "oracle", "--nlg", "bank", "--sim-phrasing", "template"]) == 0
    with pytest.raises(SystemExit):
        main(["--nlu", "oracle", "--nlg", "llm", "--sim-phrasing", "template"])


def test_split_providers_file_halves_every_rate() -> None:
    import yaml

    base = yaml.safe_load(Path("config/providers.yaml").read_text(encoding="utf-8"))
    split = yaml.safe_load(
        Path("docs/eval/ab_20261007/providers_split2.yaml").read_text(encoding="utf-8")
    )
    assert split["profiles"] == base["profiles"]
    for name, cfg in base["providers"].items():
        for key in ("rpm", "tpm"):
            if cfg.get(key):
                assert split["providers"][name][key] == max(1, cfg[key] // 2), (name, key)
