"""Sequential eval runner: scenarios → per-scenario JSON → metrics + thresholds.

CLI: ``python -m eval.run_eval --scenarios 12 --seed 7 [--resume RUN_ID]
[--profile eval] [--nlu oracle|llm] [--nlg llm|bank|template]
[--sim-phrasing llm|template] [--no-oracle-overlay]
[--agent policy|policy_h3|react|llm_only] [--providers PATH]``.

Two layers:
- ``--nlu oracle``: offline policy eval. Sim ground-truth ``TurnAnalysis``
  replaces NLU, ``offline`` profile (FakeLLM), no network or API keys;
  requires template NLG and template sim phrasing.
- ``--nlu llm`` (default): live NLU. By default sim disposition flags are
  overlaid on the LLM result; ``--no-oracle-overlay`` turns that off.

``--agent`` picks the A/B arm (``eval.agents``): ``policy`` (default; the
production orchestrator, so CI output is unchanged), ``policy_h3`` (same
policy, H3 ack / answer acts), ``react`` or ``llm_only`` (eval-only LLM
arms). The LLM arms keep ``--profile`` even under ``--nlu oracle`` (their
moves need a real LLM). Every arm gets the same leak scan, validator and
metrics; per-call results add ``agent``, ``transcript``,
``llm_calls_per_turn`` (non-sim LLM calls per agent turn) and
``turn_latency_ms``, summarised in ``run.json["arm_metrics"]``.

``--providers`` swaps ``config/providers.yaml`` for this run only (e.g. split
per-key rpm when several arms run at once: each process paces its own keys).

Writes ``eval/results/<run_id>/<scenario_id>.json`` as each finishes; resume
skips completed ``status=ok`` files and retries ``skipped_quota``. ``run.json``
records models, call share, seed, git sha, settings. Each scenario's LLM calls
(agent and sim) land in that scenario's audit db via ``llm_call_scope``. Exits
non-zero when ``eval/thresholds.yaml`` fails. Does not import voice/UI code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from app.adapter.engine_adapter import evaluate
from app.adapter.validator import validate
from app.agent.guards import rendered_guard
from app.agent.numbers import extract_tokens
from app.agent.session import CallSession
from app.config import Settings, get_settings
from app.domain.actions import Intent, Phase
from app.domain.belief import TermStatus
from app.domain.facts import Fact
from app.llm.call_audit import audit_llm_calls, llm_call_scope
from app.llm.client import LLMUnavailable, make_client
from app.store.audit import AuditLog
from eval.agents import AGENT_NAMES, make_agent
from eval.agents.arm_metrics import arm_metrics
from eval.metrics import (
    aggregate,
    check_thresholds,
    load_scenario_results,
    load_thresholds,
    write_summaries,
)
from sim.creditor import CreditorPolicy, PhrasingMode
from sim.scenarios import Scenario, TrueRules, generate, to_creditor_rules

RESULTS_ROOT = Path(__file__).resolve().parent / "results"
NlgMode = Literal["llm", "bank", "template"]
NluMode = Literal["llm", "oracle"]
_TERMINAL_INTENTS = (Intent.PROPOSE_WRAP, Intent.NO_DEAL_WRAP, Intent.ESCALATE)


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def _new_run_id(seed: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"eval_{stamp}_s{seed}"


def _private_values(scenario: Scenario) -> set[tuple[str, int | date]]:
    client = scenario.call.client
    out: set[tuple[str, int | date]] = {
        ("money", client.draft_amount_cents),
        ("money", client.current_balance_cents),
        ("money", scenario.call.bank_fee_cents),
        ("money", scenario.call.creditor_balance_cents),
        ("money", scenario.call.original_balance_cents),
    }
    for entry in client.ledger:
        out.add(("money", entry.amount_cents))
    return out


def _engine_private_values(
    scenario: Scenario,
    truth: TrueRules,
    events: list[dict[str, Any]],
) -> set[tuple[str, int | date]]:
    """Engine-derived PRIVATE figures: max affordable % and rescue amounts.

    ``max_bp`` comes from ground truth plus every affordability result the
    agent logged; rescue lump / increment from a ground-truth engine run at
    the opening ask. Independent of the agent's own guard blocklist.
    """
    out: set[tuple[str, int | date]] = set()
    if scenario.true_max_bp is not None:
        out.add(("pct", scenario.true_max_bp))
    for ev in events:
        if ev.get("type") == "affordability":
            mb = (ev.get("payload") or {}).get("max_bp")
            if mb is not None:
                out.add(("pct", int(mb)))
    rules = to_creditor_rules(
        truth,
        program_fee_pct=scenario.call.program_fee_pct,
        bank_fee_cents=scenario.call.bank_fee_cents,
    )
    summary = evaluate(scenario.call, rules, scenario.opening_ask_bp, truth.first_payment_date)
    for fact in summary.facts.private().values():
        if fact.id.startswith("rescue_") and isinstance(fact.value, int):
            out.add((fact.kind, fact.value))
    return out


def _count_leaks(
    texts: list[str],
    blocklist: set[tuple[str, int | date]],
    *,
    exempt: set[tuple[str, int | date]] | None = None,
) -> int:
    """Spoken tokens in ``blocklist`` and not in ``exempt``."""
    skip = exempt or set()
    leaks = 0
    for text in texts:
        for tok in extract_tokens(text):
            pair = tok.as_pair()
            if pair in blocklist and pair not in skip:
                leaks += 1
    return leaks


def _count_unverified(
    texts: list[str],
    public_facts: dict[str, Fact],
    creditor_numbers: set[tuple[str, int | date]],
    private_blocklist: set[tuple[str, int | date]],
    *,
    ref: date,
) -> int:
    """Independent re-scan: spoken lines that still fail rendered_guard as unverified."""
    n = 0
    for text in texts:
        rg = rendered_guard(
            text,
            public_facts,
            creditor_numbers,
            private_blocklist,
            ref=ref,
        )
        if not rg.ok and rg.reason == "unverified_number":
            n += 1
    return n


def _belief_metrics(
    scenario: Scenario,
    session: CallSession,
    truth_rules: TrueRules | None = None,
) -> dict[str, int]:
    """Score all 7 rule fields; ``truth_rules`` defaults to the scenario's hidden rules."""
    tr = truth_rules or scenario.true_rules
    truth: dict[str, Any] = {
        "max_payments": tr.max_payments,
        "min_payment_cents": tr.min_payment_cents,
        "payment_structure": tr.payment_structure,
        "first_payment_date": tr.first_payment_date,
        "max_segments": tr.max_segments,
        "max_token_pays": tr.max_token_pays,
        "min_payment_tiers": list(tr.min_payment_tiers),
    }
    correct = 0
    total = 0
    false_known = 0
    known_count = 0
    # Score every TrueRules field; only KNOWN (not ASSUMED) counts as extracted.
    for field, true_v in truth.items():
        term = session.belief.get(field)
        total += 1
        if term.status == TermStatus.KNOWN and term.value == true_v:
            correct += 1
        if term.status != TermStatus.KNOWN:
            continue
        known_count += 1
        if term.value != true_v:
            false_known += 1
    return {
        "rule_fields_correct": correct,
        "rule_fields_total": total,
        "false_known_count": false_known,
        "known_count": known_count,
    }


def _agreement_valid(
    scenario: Scenario,
    session: CallSession,
    truth_rules: TrueRules | None = None,
) -> bool | None:
    """Validate the drafted schedule under the creditor's (agreed) rules."""
    # Silent WRAP without a drafted agreement is always invalid.
    if session.neg.phase == Phase.WRAP and session.agreement is None:
        return False
    if session.agreement is None or session.last_eval is None:
        return None
    if session.last_eval.rows is None:
        return False
    tr = truth_rules or scenario.true_rules
    rules = to_creditor_rules(
        tr,
        program_fee_pct=scenario.call.program_fee_pct,
        bank_fee_cents=scenario.call.bank_fee_cents,
    )
    violations = validate(
        session.last_eval.rows,
        scenario.call.client,
        session.last_eval.offer_total_cents,
        session.last_eval.program_fee_cents,
        rules,
        tr.first_payment_date,
    )
    return violations == []


def _surplus(scenario: Scenario, agreed_bp: int | None) -> float | None:
    if agreed_bp is None or not scenario.zopa:
        return None
    if scenario.true_max_bp is None:
        return None
    denom = scenario.true_max_bp - scenario.floor_bp
    if denom <= 0:
        return None
    return (scenario.true_max_bp - agreed_bp) / denom


def _build_settings(
    *,
    profile: str,
    nlg: NlgMode,
    nlu: NluMode = "llm",
    base: Settings | None = None,
) -> Settings:
    src = base or get_settings()
    return Settings(
        groq_api_key=src.groq_api_key,
        mistral_api_key=src.mistral_api_key,
        gemini_api_key=src.gemini_api_key,
        openrouter_api_key=src.openrouter_api_key,
        cerebras_api_key=src.cerebras_api_key,
        # Suffixed key pools (GROQ_API_KEY_2, ...) and an explicit base pool.
        api_key_pool=src.api_key_pool,
        llm_key_cooldown_s=src.llm_key_cooldown_s,
        llm_profile=profile,
        # Eval must not mix cache hits into call_share / latency.
        llm_cache=False,
        llm_cache_path=src.llm_cache_path,
        nlg_mode=nlg,
        nlg_bank_path=src.nlg_bank_path,
        nlg_h3=src.nlg_h3,
        nlu_mode=nlu,
        llm_timeout_nlu_s=src.llm_timeout_nlu_s,
        llm_timeout_nlg_s=src.llm_timeout_nlg_s,
        llm_timeout_sim_s=src.llm_timeout_sim_s,
        llm_timeout_agent_s=src.llm_timeout_agent_s,
        db_path=src.db_path,
        hostility_threshold=src.hostility_threshold,
        max_turns=src.max_turns,
        max_counters=src.max_counters,
        anchor_ratio=src.anchor_ratio,
        concession_factor=src.concession_factor,
        firm_name=src.firm_name,
        opening_disclosure=src.opening_disclosure,
        close_gap_bp=src.close_gap_bp,
    )


async def run_one_scenario(
    scenario: Scenario,
    *,
    settings: Settings,
    llm: Any,
    sim_phrasing: PhrasingMode,
    audit_dir: Path,
    max_turns: int | None = None,
    oracle_overlay: bool = True,
    agent: str = "policy",
) -> dict[str, Any]:
    """Run one full text call with arm ``agent``; return a serializable result dict.

    The sim's ground-truth ``TurnAnalysis`` is always passed under
    ``nlu_mode=oracle`` (it *is* the NLU); under live NLU it is passed only
    when ``oracle_overlay`` is set (disposition flags overlay).
    """
    turns = settings.max_turns if max_turns is None else max_turns
    pass_oracle = settings.nlu_mode == "oracle" or oracle_overlay
    audit_path = audit_dir / f"audit_{scenario.id}.db"
    audit = AuditLog(audit_path)
    session = CallSession(scenario=scenario.call)
    orch = make_agent(agent, session, llm=llm, settings=settings, audit=audit)
    creditor = CreditorPolicy(scenario, phrasing=sim_phrasing, llm=llm)
    # Agent-side LLM attempts (NLU, NLG, agent steps; not the sim) this turn.
    turn_calls = [0]

    def _count_call(meta: dict[str, Any]) -> Any:
        if meta.get("role") != "sim":
            turn_calls[0] += 1
        return prior_hook(meta) if prior_hook is not None else None

    # Audit this scenario's LLM calls (sim included) into its own db; restore after.
    prior_hook = getattr(llm, "on_call", None)
    if llm is not None:
        llm.on_call = audit_llm_calls(audit, then=_count_call)
    agent_lines: list[str] = []
    public_pairs: set[tuple[str, int | date]] = set()
    timings: list[dict[str, float]] = []
    readback_count = 0
    turns_to_proposal: int | None = None
    intents: list[str] = []
    # (intent, spoken sentences) per agent turn, for identical-move detection.
    moves: list[tuple[str, tuple[str, ...]]] = []
    creditor_turns = 0
    transcript: list[dict[str, str]] = []
    llm_calls_per_turn: list[int] = []
    turn_latency_ms: list[float | None] = []

    def _record(utt: Any) -> None:
        sentences = tuple(t for _, t in utt.sentences)
        agent_lines.extend(sentences)
        transcript.extend({"role": "agent", "text": t} for t in sentences)
        llm_calls_per_turn.append(turn_calls[0])
        turn_calls[0] = 0
        turn_latency_ms.append((utt.timings or {}).get("server_total_ms"))
        for fact in utt.action.facts.values():
            if fact.visibility == "PUBLIC" and isinstance(fact.value, (int, date)):
                public_pairs.add((fact.kind, fact.value))
        intents.append(utt.action.intent.value)
        moves.append((utt.action.intent.value, sentences))
        if utt.timings:
            timings.append(dict(utt.timings))

    async def _drive_call() -> Any:
        """Opening, then creditor/agent turns until a terminal move; returns last action."""
        nonlocal readback_count, turns_to_proposal, creditor_turns
        utt = await orch.start()
        _record(utt)
        action = utt.action
        for _ in range(turns):
            if action.intent in _TERMINAL_INTENTS:
                break
            if session.neg.phase in (Phase.WRAP, Phase.ESCALATE, Phase.END):
                break
            reply = await creditor.respond(
                action, agent_text=agent_lines[-1] if agent_lines else ""
            )
            creditor_turns += 1
            transcript.append({"role": "creditor", "text": reply.text})
            turn_calls[0] = 0
            utt = await orch.on_creditor_text(
                reply.text, oracle=reply.analysis if pass_oracle else None
            )
            _record(utt)
            if utt.action.intent == Intent.READ_BACK:
                readback_count += 1
            if turns_to_proposal is None and utt.action.intent in (
                Intent.CONFIRM_SCHEDULE,
                Intent.PROPOSE_WRAP,
            ):
                turns_to_proposal = session.neg.turn_idx
            action = utt.action
            if creditor.done:
                break
        return action

    try:
        with llm_call_scope(session.call_id):
            action = await _drive_call()
    except LLMUnavailable as e:
        audit.close()
        return {
            "scenario_id": scenario.id,
            "status": "skipped_quota",
            "error": str(e),
            "stratum": scenario.stratum,
            "persona": scenario.persona,
            "zopa": scenario.zopa,
            "should_escalate": scenario.should_escalate,
        }
    except Exception as e:
        audit.close()
        return {
            "scenario_id": scenario.id,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "stratum": scenario.stratum,
            "persona": scenario.persona,
            "zopa": scenario.zopa,
            "should_escalate": scenario.should_escalate,
        }
    finally:
        if llm is not None:
            llm.on_call = prior_hook

    events = audit.for_call(session.call_id)
    guard_blocks = sum(1 for ev in events if ev.get("type") == "blocked")
    audit.close()

    truth = creditor.agreed_rules
    ended = (
        action.intent in _TERMINAL_INTENTS
        or session.neg.phase in (Phase.WRAP, Phase.ESCALATE, Phase.END)
        or creditor.done
    )
    hit_max_turns = not ended or action.reason == "max_turns"

    private = _private_values(scenario)
    # Engine-private figures can legitimately equal a spoken PUBLIC fact
    # (a counter at the ceiling, an offer total); only unsanctioned ones leak.
    engine_private = _engine_private_values(scenario, truth, events)
    ref = scenario.call.client.as_of_date
    public_facts = {
        f"hist_{i}": Fact(
            id=f"hist_{i}",
            kind=kind,  # type: ignore[arg-type]
            value=value,
            visibility="PUBLIC",
            source="engine",
        )
        for i, (kind, value) in enumerate(sorted(public_pairs, key=lambda p: (p[0], str(p[1]))))
    }
    unverified = _count_unverified(
        agent_lines,
        public_facts,
        session.creditor_numbers,
        private,
        ref=ref,
    )
    leaks = _count_leaks(agent_lines, private) + _count_leaks(
        agent_lines, engine_private - private, exempt=public_pairs
    )
    belief = _belief_metrics(scenario, session, truth)
    agr_valid = _agreement_valid(scenario, session, truth)
    identical = sum(1 for a, b in zip(moves, moves[1:], strict=False) if a == b)
    escalated = session.neg.phase == Phase.ESCALATE
    got_deal = session.agreement is not None

    return {
        "scenario_id": scenario.id,
        "status": "ok",
        "stratum": scenario.stratum,
        "persona": scenario.persona,
        "zopa": scenario.zopa,
        "should_escalate": scenario.should_escalate,
        "rescue_within_guardrail": scenario.rescue_within_guardrail,
        "phase": session.neg.phase.value,
        "final_intent": intents[-1] if intents else None,
        "intents": intents,
        "agreed_bp": session.agreed_bp,
        "true_max_bp": scenario.true_max_bp,
        "floor_bp": scenario.floor_bp,
        "opening_ask_bp": scenario.opening_ask_bp,
        "agreement_valid": agr_valid,
        "got_deal": got_deal,
        "escalated": escalated,
        "agent_lines": agent_lines,
        "timings": timings,
        "guard_blocks": guard_blocks,
        "readback_count": readback_count,
        "turns_to_proposal": turns_to_proposal,
        "unverified_figures_spoken": unverified,
        "sensitive_leaks": leaks,
        "surplus_captured": _surplus(scenario, session.agreed_bp),
        "counters_spoken": intents.count(Intent.COUNTER.value),
        "max_counters": settings.max_counters,
        "identical_consecutive_agent_moves": identical,
        "turns_to_outcome": creditor_turns,
        "hit_max_turns": hit_max_turns,
        "final_reason": action.reason,
        "agent": agent,
        "transcript": transcript,
        "llm_calls_per_turn": llm_calls_per_turn,
        "turn_latency_ms": turn_latency_ms,
        **belief,
    }


_DEFAULT_PROVIDERS = Path(__file__).resolve().parents[1] / "config" / "providers.yaml"


def _route_models(settings: Settings, path: Path = _DEFAULT_PROVIDERS) -> dict[str, list[str]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile = data["profiles"].get(settings.llm_profile, {})
    out: dict[str, list[str]] = {}
    for role in ("nlu", "nlg", "sim", "stt", "agent"):
        specs = profile.get(role) or profile.get("all") or []
        out[role] = list(specs)
    return out


def _should_skip_existing(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return data.get("status") == "ok"


async def _async_main(args: argparse.Namespace) -> int:
    seed = args.seed
    n = args.scenarios
    nlu: NluMode = args.nlu
    nlg: NlgMode = args.nlg
    sim_phrasing: PhrasingMode = args.sim_phrasing
    oracle_overlay: bool = args.oracle_overlay
    agent: str = args.agent
    # Oracle NLU is the offline policy layer: FakeLLM, no network. The LLM arms
    # still need a real model for their moves, so they keep --profile.
    policy_arm = agent in ("policy", "policy_h3")
    profile = "offline" if nlu == "oracle" and policy_arm else args.profile

    run_id = args.resume or _new_run_id(seed)
    run_dir = RESULTS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    audit_dir = run_dir / "audit"
    audit_dir.mkdir(exist_ok=True)

    settings = _build_settings(profile=profile, nlg=nlg, nlu=nlu)
    call_counts: Counter[str] = Counter()

    def on_call(meta: dict[str, Any]) -> None:
        if meta.get("cache_hit"):
            return
        provider = meta.get("provider", "?")
        model = meta.get("model", "?")
        call_counts[f"{provider}/{model}"] += 1

    providers = Path(args.providers) if args.providers else _DEFAULT_PROVIDERS
    llm = make_client(settings, on_call=on_call, providers_path=providers)
    scenarios = generate(n, seed)

    print(
        f"run_id={run_id} scenarios={len(scenarios)} profile={profile} "
        f"nlu={nlu} nlg={nlg} sim={sim_phrasing} oracle_overlay={oracle_overlay} agent={agent}"
    )

    for i, sc in enumerate(scenarios, 1):
        out_path = run_dir / f"{sc.id}.json"
        if _should_skip_existing(out_path):
            print(f"[{i}/{len(scenarios)}] skip {sc.id} (resume)")
            continue
        print(f"[{i}/{len(scenarios)}] run  {sc.id} ({sc.stratum}/{sc.persona}) ...", flush=True)
        result = await run_one_scenario(
            sc,
            settings=settings,
            llm=llm,
            sim_phrasing=sim_phrasing,
            audit_dir=audit_dir,
            oracle_overlay=oracle_overlay,
            agent=agent,
        )
        out_path.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"         → {result.get('status')} phase={result.get('phase')}", flush=True)

    await llm.aclose()

    results = load_scenario_results(run_dir)
    summary = aggregate(results)
    total_calls = sum(call_counts.values())
    call_share = {
        k: (v / total_calls if total_calls else 0.0) for k, v in sorted(call_counts.items())
    }
    run_meta: dict[str, Any] = {
        "run_id": run_id,
        "seed": seed,
        "n_scenarios": n,
        "git_sha": _git_sha(),
        "profile": profile,
        "nlu": nlu,
        "nlg": nlg,
        "sim_phrasing": sim_phrasing,
        "oracle_overlay": oracle_overlay,
        "agent": agent,
        "arm_metrics": arm_metrics(results),
        "settings": {
            "llm_profile": settings.llm_profile,
            "nlg_mode": settings.nlg_mode,
            "nlg_h3": agent == "policy_h3" or settings.nlg_h3,
            "nlu_mode": settings.nlu_mode,
            "max_turns": settings.max_turns,
            "max_counters": settings.max_counters,
            "anchor_ratio": settings.anchor_ratio,
            "concession_factor": settings.concession_factor,
            "hostility_threshold": settings.hostility_threshold,
            "llm_cache": settings.llm_cache,
        },
        "models": _route_models(settings, providers),
        "providers": str(providers),
        "call_counts": dict(call_counts),
        "call_share": call_share,
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_meta, indent=2, default=str) + "\n", encoding="utf-8"
    )
    write_summaries(run_dir, summary, run_meta=run_meta)
    print(f"arm_metrics ({agent}): {json.dumps(run_meta['arm_metrics'])}")

    thresholds = load_thresholds()
    failures = check_thresholds(summary, thresholds)
    run_meta["thresholds"] = thresholds
    run_meta["thresholds_passed"] = not failures
    run_meta["threshold_failures"] = failures
    (run_dir / "run.json").write_text(
        json.dumps(run_meta, indent=2, default=str) + "\n", encoding="utf-8"
    )

    md = (run_dir / "summary.md").read_text(encoding="utf-8")
    print("\n" + md)
    if failures:
        print("THRESHOLD FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("thresholds: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Debt Settlement Agent eval runner")
    p.add_argument("--scenarios", type=int, default=12)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--resume", type=str, default=None, metavar="RUN_ID")
    p.add_argument("--profile", type=str, default="eval")
    p.add_argument(
        "--nlg",
        choices=("llm", "bank", "template"),
        default="template",
        help="NLG mode (default template = cheap smoke; bank = demo; llm = live phrasing)",
    )
    p.add_argument(
        "--sim-phrasing",
        choices=("llm", "template"),
        default="llm",
        dest="sim_phrasing",
    )
    p.add_argument(
        "--nlu",
        choices=("llm", "oracle"),
        default="llm",
        help="oracle = offline policy eval (sim ground truth as NLU, FakeLLM, no network)",
    )
    p.add_argument(
        "--no-oracle-overlay",
        action="store_false",
        dest="oracle_overlay",
        help="live NLU only: do not overlay sim disposition flags on the LLM result",
    )
    p.add_argument(
        "--agent",
        choices=AGENT_NAMES,
        default="policy",
        help=(
            "A/B arm: policy (production, default) | policy_h3 (policy + H3 acts) "
            "| react | llm_only (eval-only LLM arms)"
        ),
    )
    p.add_argument(
        "--providers",
        type=str,
        default=None,
        metavar="PATH",
        help="providers.yaml for this run (default config/providers.yaml)",
    )
    args = p.parse_args(argv)
    if args.nlu == "oracle":
        if args.nlg == "llm" or args.sim_phrasing != "template":
            p.error("--nlu oracle requires --nlg template|bank --sim-phrasing template")
        if not args.oracle_overlay:
            p.error("--no-oracle-overlay applies to --nlu llm only")
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
