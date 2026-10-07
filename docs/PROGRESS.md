# Debt Settlement Agent progress log

Each phase appends its handoff here. Keep entries short: facts later phases need, not narration.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Scaffold and vendored engine | done |
| 1 | Domain model | done |
| 2 | Engine adapter and validator | done |
| 3 | Numbers and guards | done |
| 4 | Policy, NLG templates, audit | done |
| 5 | LLM client and provider pool | done |
| 6 | NLU and LLM NLG | done |
| 7 | Session, orchestrator, CLI | done |
| 8 | Simulator, scenarios, offline e2e | done |
| 9 | Eval runner and metrics | done |
| 10 | Voice and UI | done |
| 10.1 | Demo UX + non-price recovery | done |
| 11 | README and final eval | done |
| 12 | Honest offline policy eval (ROADMAP) | done |
| 13 | CI + frozen evidence (ROADMAP) | done |
| 14 | `decide()` bugs, bounded extract, invariants (ROADMAP) | done |
| 15 | NLU and safety corpus, flag fixes | done |
| 16 | LLM-only baseline (ROADMAP) | not done |
| 17 | Latency timeouts + measurement (ROADMAP) | not done |
| 18 | LLM audit log + replay CLI (ROADMAP) | not done |
| 19 | Results-first README (ROADMAP) | done |
| 20 | Correctness and honesty fixes (REVIEW_PLAN) | done |
| 21 | Latency: measure then cut (REVIEW_PLAN) | done |
| 22 | Decision trace, role-scoped streams, autoplay (REVIEW_PLAN) | done |
| 23a | Web call console: scaffold and components (REVIEW_PLAN) | done |
| 23b | Web call console: wire-up and cutover (REVIEW_PLAN) | done (voice call not run by hand, see handoff) |
| 24a | A/B harness, ReAct and LLM-only arms (REVIEW_PLAN) | done |
| 24b | H3 conversational NLG + the A/B run (REVIEW_PLAN) | code merged; A/B WIP (arm C resumes 2026-10-08, D dropped) |
| 25 | Recruiter packaging (REVIEW_PLAN) | done (hosted-demo check pending a deploy; A/B placeholders pending 24b) |
| 26 | Carry-over cleanup | done |
| 27 | Provider API key pool (user request) | done |
| 28 | Filler false-accept veto | in progress (parallel worktree) |

## Environment facts
- Engine timing (measured before phase 0): a 100-point settlement scan takes 17–261 ms per case.
- Feasibility by settlement % is non-monotonic (for example `0000111100001111...`), and low percentages fail on payment floors.
- Ollama 0.30.10 is installed. Pull `qwen3.5:9b` and `gemma4:e4b` before using the `local` profile.
- Python 3.12 via `uv venv --python 3.12`. Ruff excludes vendored `feasibility/`.
- Demo affordability scan (`range(100, 10001, 100)`, even rules, `fixtures/demo`): median **8.3 ms** cold (5 runs: 8.0–8.5 ms); `lru_cache` hit is sub-ms. `max_bp=10000`, 99/100 grid points feasible.

## Interfaces

### `app.config`
- `class Settings(BaseSettings)` — fields: `groq_api_key`, `mistral_api_key`, `gemini_api_key`, `openrouter_api_key`, `cerebras_api_key` (`str | None`); `llm_profile` (`str`, default `"demo"`); `llm_timeout_nlu_s` / `llm_timeout_nlg_s` / `llm_timeout_stt_s` / `llm_timeout_sim_s` (`float`, 6 / 4 / 8 / 15); `llm_cache` (`bool`); `llm_cache_path` (`str`); `nlg_mode` (`llm` | `bank` | `template`) / `nlu_mode` (`str`); `nlg_bank_path` (`str`, `config/nlg_bank.json`); `db_path` (`str`); `hostility_threshold` (`float`); `max_turns` / `max_counters` (`int`); `anchor_ratio` / `concession_factor` (`float`); `firm_name` / `opening_disclosure` (`str`).
- Phase 24b: `nlg_h3: bool = False` (ack / answer acts + 3-turn NLG context); `llm_timeout_agent_s: float = 20.0` (eval A/B agent role).
- `get_settings() -> Settings`

### `app.domain.units`
- `render_money(cents: int) -> str`
- `parse_money(text: str) -> int`
- `render_pct(bp: int) -> str`
- `parse_pct(text: str) -> int`
- `render_date(d: date, ref: date) -> str`
- `parse_date(text: str, ref: date) -> date`
- `render_count(n: int) -> str`
- `render_ordinal(n: int) -> str` — `1st`, `2nd`, `11th`, `22nd`
- `parse_count(text: str) -> int`
- `bp_to_decimal(bp: int) -> Decimal`

### `app.domain.fields`
- `FieldKind = Literal["int", "cents", "enum", "date", "tiers"]`
- `PaymentStructure = Literal["even", "balloon", "flexible"]`
- `class FieldSpec` — `name`, `kind`, `ask_text`, `readback_text`, `required: bool`, `default_factory: Callable[[], Any] | None`, `prior_range: tuple[Any, ...] | None`
- `FIELD_REGISTRY: list[FieldSpec]` — `max_payments`, `min_payment_cents`, `payment_structure`, `first_payment_date`, `max_segments`, `max_token_pays`, `min_payment_tiers`
- `FIELDS_BY_NAME: dict[str, FieldSpec]`
- `REQUIRED_FIELDS: list[str]`

### `app.domain.facts`
- `class Fact(BaseModel)` — `id: str`, `kind: Literal["money","pct","count","date","ordinal"]`, `value: int | date`, `visibility: Literal["PUBLIC","PRIVATE"]`, `source: Literal["engine","creditor","config"]`; `render(self, ref: date) -> str`
- `class FactSet(BaseModel)` — `facts: dict[str, Fact]`; `add(fact)`, `get(id) -> Fact | None`, `public() -> dict[str, Fact]`, `private() -> dict[str, Fact]`, `ids() -> set[str]`

### `app.domain.belief`
- `class TermStatus(StrEnum)` — `UNKNOWN`, `TENTATIVE`, `KNOWN`, `CONTRADICTED`, `ASSUMED`
- `class Evidence(BaseModel)` — `turn: int`, `quote: str`
- `class TermBelief(BaseModel)` — `field`, `value`, `status`, `evidence`, `history`
- `class BeliefChange(BaseModel)` — `field`, `old_value`, `new_value`, `old_status`, `new_status`, `turn`, `quote`
- `class BeliefState` — `__init__(client: Client)`; `get(field) -> TermBelief`; `observe(field, value, quote, turn, *, verified, hedged) -> BeliefChange`; `confirm_readback(field, yes: bool) -> BeliefChange`; `usable_for_engine(field) -> bool`; `missing_required() -> list[str]`; `tentative_fields() / contradicted_fields() -> list[str]`
- ASSUMED seeds: `first_payment_date=default_first_payment_date(client)`, `max_segments=2`, `min_payment_tiers=[]`, `max_token_pays` synced to `max_payments` while ASSUMED

### `app.domain.scenario`
- `class CallScenario` — `id`, `client: Client`, `creditor`, `creditor_balance_cents`, `original_balance_cents`, `program_fee_pct: float`, `bank_fee_cents: int`
- `load_scenario(path: str | Path) -> CallScenario` — reads `client.json`, `offer.json`, `firm.json`

### `app.adapter.engine_adapter`
- `class NeedsInfo(Exception)` — `fields: list[str]`
- `class EvalSummary` — `feasible`, `shape`, `rows`, `additional_funds`, `assumed_fields`, `facts: FactSet`, `offer_total_cents`, `program_fee_cents`
- `class Affordability` — `max_bp: int | None`, `feasible_bps: list[int]`, `curve: tuple[bool, ...]` (100 pts for `range(100, 10001, 100)`)
- `build_rules(belief: BeliefState, firm: FirmFees) -> CreditorRules` — raises `NeedsInfo` if any required field not usable
- `assumed_fields(belief: BeliefState) -> list[str]`
- `evaluate(scenario, rules, bp, first_payment_date, *, assumed=None) -> EvalSummary` — PUBLIC: offer_total, num_payments, first_payment_date, last_payment, payment_level_*; PRIVATE: balances, program/bank fees, rescue amounts
- `affordability(scenario, rules, fpd) -> Affordability` — `lru_cache` on `(scenario_key, rules_tuple, fpd)`
- `clear_affordability_cache() -> None`

### `app.adapter.validator`
- `class Violation` — `rule: str`, `message: str`
- `BINDING_RULES` — 10 ids: `cadence`, `exact_sum`, `non_decreasing`, `floors`, `token_count`, `even_vector`, `segments`, `fees_and_horizon`, `ledger_nonnegative`, `balance_match`
- `validate(schedule_rows, client, offer_total, program_fee, rules, first_payment_date) -> list[Violation]` — no imports of `feasibility.simulate` / `shapes` / `scoring`

### `app.agent.numbers`
- `NUMBER_WORDS: frozenset[str]` — zero..twenty, thirty..ninety, hundred/thousand/million, first..twelfth, half, quarter, dozen, couple
- `ONE_ALLOWLIST_PHRASES` — `no one`, `one moment`, `one more`, `one second`
- `class NumberToken` — `kind` (`money`|`pct`|`count`|`date`|`ordinal`), `value: int | date`, `raw`, `start`, `end`; `as_pair() -> tuple[str, int | date]`
- `words_to_number(text: str) -> int | None` — common forms including `two hundred fifty`, `twenty-five hundred`
- `extract_tokens(text: str, *, ref: date | None = None) -> list[NumberToken]` — order: money `$…`, abbrev `2.5k`, pct, dates (named/ISO/slash), ordinals, bare, number words (span-masked); `N dollars` → money
- `normalize_token(token: NumberToken) -> tuple[str, int | date]`

### `app.agent.guards`
- `class GuardResult` — `ok: bool`, `reason: str`, `offending: list[str]`
- `template_guard(text, allowed_ids, required_ids) -> GuardResult` — reasons: `digit`, `dollar`, `percent`, `number_word`, `unknown_placeholder`, `missing_required`
- `rendered_guard(text, public_facts, creditor_numbers, private_blocklist, *, ref=None) -> GuardResult` — reasons: `unverified_number`, `boundary`, `commitment`; money↔count dollar cross-match; public/private collision allowed

### `app.domain.actions`
- `Phase`, `Intent` (StrEnums); `Effect`, `Action` — shared with sim (sim must not import `app.agent`)
- Phase 24b: `Intent.ANSWER` (only ever an attached act, never `decide()`'s move); `class AnswerAct(topic: str, text: str)`; `Action.ack: dict[str, Fact]` (ids `ack_max_payments` count, `ack_min_payment` money, `ack_first_payment_date` date; PUBLIC, `source="creditor"`) and `Action.answer: AnswerAct | None`. The sim ignores both.
- Re-exported from `app.agent.policy` for existing callers

### `app.domain.nlu_types`
- `ExtractedTerm`, `TurnAnalysis` — shared with sim/oracle; re-exported from `app.agent.nlu_types`
- Phase 24b: `QuestionTopic = Literal["why_not_higher","next_steps","who_approves","timeline","other"]`, `QUESTION_TOPICS`; `TurnAnalysis.asks_question: bool = False`, `question_topic: QuestionTopic | None = None` (NLU-sourced; `decide()` ignores them; the oracle overlay does not touch them)

### `app.agent.nlu_types`
- Re-exports `ExtractedTerm`, `TurnAnalysis` from `app.domain.nlu_types`

### `app.agent.policy`
- `Phase`, `Intent`, `Effect`, `Action` re-exported from `app.domain.actions`
- `Agreement`, `NegotiationState` (also `last_confirm_key`, `confirm_rejects`, `assumed_asked`, `clarify_counts`; no `rejects` since Phase 14)
- `ask_pct_to_bp(pct: float) -> int`
- `next_counter(*, ask_bp, max_bp, feasible_bps, c_prev, anchor_ratio, concession_factor) -> int`
- `decide(belief, neg, analysis, afford, *, settings=None, rescue_within_guardrail=False, confirm_facts=None, counter_offer_total_cents=None) -> Action`
  - Identical CONFIRM: `reject` → ASK each ASSUMED once then NO_DEAL; other → soft retry then `confirm_unacked`
  - Unresolved CONTRADICTED after 2 CLARIFY → `ESCALATE(contradiction_unresolved)`
  - Ceiling ladder (ask above ceiling / off grid): at most `max_counters` COUNTERs, the last one at the ceiling; no same-bp re-offer — any non-accept once the ceiling is on the table → `NO_DEAL(max_counters)`
  - CONFIRM wrap only on `stance == "accept"` (not `readback_response`); contradiction/tentative before wrap. Exception: a READ_BACK that preempts a CONFIRM accept keeps `Phase.CONFIRM` and emits `note_confirm_accepted` (→ `NegotiationState.accepted_confirm_key`); the readback "confirm" then wraps (fingerprint unchanged) or re-confirms `confirmed_bp` as `terms_revised`
  - Tiers speak via `template_override` over PUBLIC per-tier facts `<prefix>_tier_<a|b|…>_from` (ordinal) / `_min` (money), prefix `readback_value` / `clarify_old` / `clarify_new`: "a minimum of $75 from the 4th payment on" joined by "and"; empty → "no special payment tiers". Ids are lettered because placeholder ids must be digit-free.
  - `TurnAnalysis.tiers_ambiguous` → CLARIFY(`tiers_ambiguous`, number-free ask to restate from which payment); after two (`clarify_counts["min_payment_tiers"]`) → `ESCALATE(tiers_unresolved)`
  - `wants_to_end` → NO_DEAL when not accepting
  - `_confirm_key` fingerprints all CreditorRules fields; `next_counter` → `None` when no legal bp
- `draft_agreement(*, creditor, bp, offer_total, rows, assumed_fields, audit=None, call_id=None) -> Agreement`
- `opening_action(*, settings=None, firm_name=None, opening_disclosure=None) -> Action`

### `app.agent.acts` (Phase 24b)
- `ANSWER_POINTS: dict[str, str]` — number-free talking point per topic; `ACK_FIELDS` (field → (fact id, kind)); `NO_ACK_INTENTS`, `NO_ANSWER_INTENTS`
- `ack_facts(belief_changes, creditor_numbers, private_blocklist) -> dict[str, Fact]` — terms that became KNOWN or changed this turn, value creditor-said (`_cross_match`), never colliding with the private blocklist
- `answer_act(action, analysis) -> AnswerAct | None` — None on a private-info ask, for `NO_ANSWER_INTENTS`, and for redundant (topic, move) pairs
- `attach_acts(action, analysis, belief_changes, *, creditor_numbers, private_blocklist) -> Action` — copy with `ack` / `answer`; intent, facts, effects, reason unchanged

### `sim.personas`
- `PersonaName = Literal["flexible", "contradictory", "pressuring"]`
- `PERSONAS`, `PERSONA_BY_NAME`, `get_persona(name) -> Persona`
- `Persona(name, contradict_once, pressure_private_turns, pressure_commit_turns)`

### `sim.scenarios`
- `Stratum = Literal["deal", "rescue", "no_fix"]`; `STRATA`
- `TrueRules` — hidden max_payments / min_payment_cents / payment_structure / fpd / segments / token / tiers
- `Scenario` — `id`, `call: CallScenario`, `true_rules`, `opening_ask_bp`, `floor_bp`, `persona`, `true_max_bp`, `feasible_bps`, `zopa`, `rescue_within_guardrail`, `should_escalate`, `stratum`
- `to_creditor_rules(rules, *, program_fee_pct, bank_fee_cents) -> CreditorRules`
- `generate(n, seed) -> list[Scenario]` — deterministic, strata balanced
- `generate_one(persona, stratum, seed) -> Scenario`
- `scenario_from_truth(call, true_rules, *, opening_ask_bp, floor_bp, persona) -> Scenario` (Phase 22) — labels via the same `_classify` as generated cases
- `stratum_counts(scenarios)`, `balanced_quota(n)`

### `sim.creditor`
- `CreditorReply(text, analysis: TurnAnalysis)`
- `LATE_FIELDS = ("max_segments", "max_token_pays", "min_payment_tiers")`
- `CreditorPolicy(scenario, phrasing="template"|"llm", llm=None)`
  - `async respond(action, agent_text="") -> CreditorReply` — sees intent + PUBLIC facts only; validates `CONFIRM_SCHEDULE` under agreed rules; concedes 500 bp per rejected counter down to floor; emits oracle `TurnAnalysis`
  - Reveals all 7 fields: core four at OPENING; asked field on ASK; unspoken `LATE_FIELDS` prefixed to READ_BACK / CONFIRM_SCHEDULE / SPEAK_SCHEDULE replies
  - `COUNTER_TERMS`: accept iff alt is inside hidden limits (fpd ≤ true, min ≥ true, max ≤ true), else reject (no rule restatement)
  - `agreed_rules -> TrueRules` — true rules + accepted alternatives (eval ground truth)

### `eval.metrics`
- `RATE_METRICS` — rates reported with `<rate>_n` and `<rate>_ci95`
- `wilson_interval(k, n, z=1.96) -> tuple[float, float] | None`
- `load_scenario_results(run_dir) -> list[dict]`
- `aggregate(results) -> dict` — PLAN §10 metrics + latency p50/p95; empty denom → null (never vacuous 1.0); WRAP-sans-agreement counts invalid. Quality: `counters_spoken_max`, `counters_spoken_mean`, `max_counters`, `identical_consecutive_agent_moves`, `turns_to_outcome` (mean, non-stuck), `stuck_calls`, `stuck_rate`
- `write_summaries(run_dir, summary, *, run_meta=None) -> (summary.json, summary.md)` — md table `metric | value | n | 95% CI`
- `load_thresholds(path=None) -> dict`
- `check_thresholds(summary, thresholds=None) -> list[str]` (empty ⇒ pass); RHS may name another summary key (`counters_spoken_max: "<=max_counters"`); missing key fails closed

### `eval.run_eval`
- CLI: `python -m eval.run_eval --scenarios N --seed S [--resume RUN_ID] [--profile] [--nlu oracle|llm] [--nlg llm|bank|template] [--sim-phrasing llm|template] [--no-oracle-overlay] [--agent policy|policy_h3|react|llm_only] [--providers PATH]`
  - Phase 24b: `--nlg bank`; `--providers` swaps `config/providers.yaml` for one run (`run.json["providers"]`); `--nlu oracle` accepts `--nlg template|bank`; `policy_h3` is offline under `--nlu oracle` like `policy`; `_build_settings` now carries `api_key_pool`, `llm_key_cooldown_s`, `nlg_bank_path`, `nlg_h3` and the role timeouts from `base`
  - `--agent` (Phase 24a, default `policy`): LLM arms keep `--profile` under `--nlu oracle` (only policy is forced `offline`)
  - `--nlu oracle`: forces profile `offline` (FakeLLM), no network/keys; requires `--nlg template --sim-phrasing template`; rejects `--no-oracle-overlay`
  - `--nlu llm` (default): live NLU, sim disposition overlay on unless `--no-oracle-overlay`
- `_build_settings(*, profile, nlg, nlu="llm", base=None) -> Settings`
- `run_one_scenario(scenario, *, settings, llm, sim_phrasing, audit_dir, max_turns=None, oracle_overlay=True, agent="policy") -> dict` — Phase 24a adds `agent`, `transcript` (`[{role, text}]`), `llm_calls_per_turn` (non-sim attempts per agent turn), `turn_latency_ms`; `run.json` adds `agent`, `arm_metrics`; per-call keys also add `counters_spoken`, `max_counters`, `identical_consecutive_agent_moves`, `turns_to_outcome`, `hit_max_turns`, `final_reason`
- Leak scan = client/firm private amounts ∪ engine-private (`true_max_bp`, every logged affordability `max_bp`, true-rules rescue lump/increment); engine-private values that were spoken as a PUBLIC fact are exempt
- Writes `eval/results/<run_id>/<scenario_id>.json` per finish; resume skips `status=ok`, retries `skipped_quota`
- `run.json`: models, call_share, seed, git sha, settings, `nlu`, `oracle_overlay`; exit 1 on threshold fail

### `eval.agents` (Phase 24a, eval-only; never imported by `app/`)
- `AgentName = Literal["policy", "policy_h3", "react", "llm_only"]`; `AGENT_NAMES` (Phase 24b `policy_h3` = `PolicyAgent` with `nlg_h3=True`, `.name == "policy_h3"`)
- `class AgentUnderTest(Protocol)` — `session: CallSession`; `async start() -> Utterance`; `async on_creditor_text(text, timings=None, *, oracle=None) -> Utterance`; `async on_sentence_done(ids) -> Agreement | None`
- `class PolicyAgent(session, *, llm, settings, audit)` — wraps `Orchestrator(..., auto_ack=True)` unchanged; `.orchestrator`
- `make_agent(name, session, *, llm, settings, audit) -> AgentUnderTest` — `react` / `llm_only` imported lazily; LLM arms raise `ValueError` when `llm is None`
- `eval.agents.base`: `AGENT_ROLE = "agent"` (Phase 24b), `AGENT_MAX_TOKENS = 1200`, `OBSERVE_TOOLS`, `TERMINAL_TOOLS`, `MOVE_DOCS`, `NEGOTIATION_RULES`, `MoveError`, `Move(tool, args, text)`, `parse_json_object(raw) -> dict`, `coerce_bp(value) -> int`; `class LLMArmAgent` — `guarded: bool`, `decide() -> (Action, str)` (subclass), `build_action(move) -> Action`, `tool_get_rules()`, `async tool_evaluate_offer(args)`, `context_block() -> str`, `fallback_action(reason)`, `last_turn_llm_calls`, `afford`
- `eval.agents.react_agent`: `MAX_STEPS = 4`, `SYSTEM_PROMPT`, `class ReactAgent(LLMArmAgent)` (guarded)
- `eval.agents.llm_only_agent`: `SYSTEM_PROMPT`, `class LLMOnlyAgent(LLMArmAgent)` (unguarded, one call, no retry)
- `eval.agents.arm_metrics.arm_metrics(results) -> dict` — `turns`, `llm_calls_per_turn_{mean,p95,max}`, `turn_latency_ms_{p50,p95}`

### `eval.ab_report` (Phase 24b; not a gate)
- CLI `python -m eval.ab_report --arm NAME=RUN_DIR ... --judge X:Y=JUDGE_DIR ... --out DIR [--decision FILE] [--notes FILE]` (first arm = baseline A) → `summary.md` / `summary.json`
- `common_ids(arms)`, `outcome(r)`, `pick_transcripts(results) -> (representative, worst)`, `arm_row(results)`, `adoption_checks(row, base, win_rate) -> dict[str, bool]` (fails closed), `build_report(arm_dirs, judges, *, decision="", notes="") -> (md, data)`

### `eval.judge_naturalness` (Phase 24a; not a gate)
- CLI `python -m eval.judge_naturalness RUN_A RUN_B [--profile eval] [--limit N] [--seed 0] [--human-pairs 20] [--out DIR]`
- `paired_results(run_a, run_b)`, `async judge_pair(llm, text_a, text_b) -> {a_first, b_first, verdict}`, `summarize(verdicts) -> dict` (Wilson CI over decisive pairs), `write_human_pairs(out_dir, pairs, *, n, seed, label_a, label_b)`, `async run_judge(run_a, run_b, *, llm, out_dir, limit=None, seed=0, human_pairs=20) -> dict`
- Judge calls use role `sim`; a win needs both orders to agree, else tie

### `app.agent.nlg`
- `SAFE_FALLBACK: str`; `TEMPLATES: dict[Intent, str]`
- `render_action(action, ref_date, *, creditor_numbers=None, private_blocklist=None, audit=None, call_id=None) -> list[str]` — deterministic template path
- Phase 22: `render_action(..., trace_out=None)` / `speak_action(..., trace_out=None)` — optional dict filled with `mode`, `source` (`default`|`override`|`bank`|`llm`), `template`, `guards` (`[{stage, ok, reason, offending}]`), `fallback_used`, `fallback_reason` (`safe_fallback`|`bank_miss`|`llm_template_rejected`|`llm_unavailable`); never changes the spoken text
- Phase 24b: `ACK_TEMPLATES: dict[tuple[ids], str]` (7 id sets), `ANSWER_TEMPLATE = TEMPLATES[Intent.ANSWER] = "{answer_text}"`, `ACK_BANK_INTENT = "ACK"`, `answer_bank_intent(topic) -> "ANSWER:<topic>"`, `NLG_MAX_TOKENS = 800`; `render_acts(action, ref_date, *, nlg_mode="template", bank_path=None, creditor_numbers=None, private_blocklist=None, audit=None, call_id=None, blocked_out=None, turn=0) -> list[str]` — ack then answer, each through `template_guard` + `rendered_guard`; bank/llm modes use bank variants (never an LLM call); a failing act is dropped and audited `nlg/act_dropped`. `speak_action(..., recent_turns=None)`; an empty LLM template counts as rejected (falls back to `TEMPLATES`, `fallback_reason=llm_template_rejected`)
- `async speak_action(action, ref_date, *, llm=None, settings=None, last_rep_line="", creditor_numbers=None, private_blocklist=None, audit=None, call_id=None, blocked_out=None, turn=0) -> list[str]` — `nlg_mode=bank`: bank template (no LLM); `llm`: LLM template → template_guard (1 retry); both fall back to `TEMPLATES` → fill → rendered_guard; raises `LLMUnavailable` (Phase 20; orchestrator `_speak` falls back and audits `llm_unavailable`)

### `app.agent.nlg_bank` (Phase 21)
- `BankKey = tuple[str, tuple[str, ...]]`; `bank_key(intent, placeholder_ids) -> BankKey`; `action_placeholder_ids(action) -> set[str]`
- `parse_bank(data) -> dict[BankKey, list[str]]`; `load_bank(path=DEFAULT_BANK_PATH)` (lru_cache; missing file → `{}`)
- `pick_template(action, *, call_id, turn, bank, intent_key=None) -> str | None` (Phase 24b `intent_key`: `ACK` / `ANSWER:<topic>`) — candidates passing `template_guard` with the action's ids; index `sha256(f"{call_id}:{turn}") mod n`
- `config/nlg_bank.json`: `{generated, profile, per_key, stats, entries: [{intent, placeholders, required, templates}]}`; built by `scripts/build_template_bank.py`; Phase 24b adds `ACK` (7 id sets) and `ANSWER:<topic>` (5, no placeholders, talking point always first) via `--acts` (merge; other entries kept)

### `app.agent.nlu`
- `repair_stance(stance, utterance, *, has_terms=False) -> str` — injection never accepts → reject phrase → accept phrase → dominant short ack with no number/term
- `repair_asks_client_private_info` / `repair_demands_commitment(claimed, utterance) -> bool` — LLM flag OR un-negated regex cue
- `class VerifiedTerm` — `field`, `value`, `quote`, `hedged`, `verified`
- `class VerifiedAnalysis` — terms + TurnAnalysis stance fields + `ask_verified`; `to_turn_analysis() -> TurnAnalysis`
- `normalize_for_quote(text) -> str`; `quote_in_utterance(quote, utterance) -> bool`
- `coerce_analysis_payload(data) -> dict` — field-keyed LLM shapes → `terms[]`
- `coerce_tiers(raw) -> list[tuple[int, int]] | None` — `{"from_payment","min_cents"}` dicts or 2-int pairs → sorted engine tuples; None (term dropped, `nlu_rejected_tiers` audited) on any bad item, extra/other keys, `from_payment < 1`, `min_cents <= 0`, or duplicate `from_payment`
- `VerifiedAnalysis.tiers_ambiguous` — non-empty tiers in an utterance with "first/initial … payments" are dropped (`nlu_tiers_ambiguous`) instead of converted
- `post_verify(analysis, utterance, *, ref=None, audit=None, call_id=None) -> VerifiedAnalysis`
- Phase 22: `class DroppedTerm` — `field`, `value`, `reason` (audit event minus `nlu_`: `rejected_quote`, `rejected_tiers`, `tiers_ambiguous`, `cents_ambiguity`, `rejected_range`, `rejected_date`, `rejected_bare_year`, `rejected_ask_value`), `quote`; `VerifiedAnalysis.dropped: list[DroppedTerm]` (trace only; policy never reads it)
- `async analyze(utterance, last_agent_line, pending_readback, *, llm=None, settings=None, oracle=None, audit=None, call_id=None, ref=None) -> VerifiedAnalysis`
- Phase 24b: `repair_question(asks_question, topic, utterance, *, asks_private) -> (bool, str | None)` — never on a private ask; LLM flag needs `?` or an interrogative opener; topic regex cues flag alone and beat an LLM `other`; an `other` question mentioning terms / figures is dropped (on-script). `VerifiedAnalysis.asks_question` / `question_topic`; `coerce_analysis_payload` maps an off-list topic to `other`
- `NLU_MODE=oracle` requires `oracle=TurnAnalysis` (skips LLM); re-raises `LLMUnavailable` for eval `skipped_quota`

### `app.agent.session`
- `class Turn` — `role` (`agent`|`creditor`), `text`, `spoken`, `sentence_id`
- `class PendingSpeech` — `action`, `sentence_ids`, `acked`
- `class CallSession` — `call_id`, `scenario`, `belief`, `neg: NegotiationState`, `history`, `pending`, `last_eval`, `agreed_bp`, `agreement`, `last_max_bp`, `creditor_numbers`, `private_blocklist`, `last_belief_changes`, `last_blocked`; props `phase`, `turn_idx`; `last_agent_line()`

### `app.agent.orchestrator`
- `class Utterance` — `sentences: list[tuple[id, text]]`, `action`, `timings`, `belief_changes`, `agreement`, `trace: TurnTrace | None` (Phase 22; set on every emitted utterance, full operator data)
- `apply_effects(session, effects) -> None`
- `class Orchestrator(session, *, llm=None, settings=None, audit=None, auto_ack=False)`
  - `async start() -> Utterance`
  - `async on_creditor_text(text, timings=None, *, oracle=None) -> Utterance` — cancel-and-merge during NLU (merged callers get the same `Utterance`); queue after NLU; affordability via `asyncio.to_thread`; timings `nlu_ms`/`engine_ms`/`policy_ms`/`nlg_ms`/`queue_ms`/`server_total_ms` (Phase 21: `engine_ms` = `_engine_context`; `queue_ms` = rate-limit wait inside the other stages, not additive)
  - `pop_drained() -> list[Utterance]` — turns run by the post-NLU drain inside `on_sentence_done` (Phase 21; the WS emits them)
  - `async on_sentence_done(ids) -> Agreement | None` — commits effects when all pending sentences acked; drafts agreement on `PROPOSE_WRAP` after validator pass
  - `async on_barge_in(spoken_ids) -> None` — drops pending effects; keeps belief
  - Phase 24b: with `settings.nlg_h3`, `attach_acts` runs after `decide` / `_enrich_action` (audit `policy/decide` gains `acts{ack, answer}`); `_speak` = `render_acts` sentences + the move's sentences; `_recent_public_turns()` feeds `speak_action(recent_turns=)` (spoken lines only, a line with any private-blocklist figure dropped)

### `app.agent.reasons` (Phase 22)
- `REASON_TEXT: dict[str, str]` — one sentence per reason key; `{placeholders}` only from `PUBLIC_PLACEHOLDERS` (`counter_pct`, `settlement_pct`, `offer_total`, `num_payments`, `first_payment_date`, `alt_first_payment_date`, `alt_min_payment_cents`, `alt_max_payments`, `field_label`)
- `reason_key(intent, reason) -> str` — `bp=N` → `counter` / `confirm`; field-name reasons → `ask_field` / `read_back` / `clarify_field`; `None` → `opening` / `ask_settlement` / `confirm` / `counter`; literals unchanged
- `reason_text(action, ref) -> str` — fills from PUBLIC facts + field label; a missing value reads "that value"; an unknown key gives "The policy chose <intent>." Phase 24b: a COUNTER without `offer_total` uses display key `counter_no_total` (reason code unchanged)

### `app.schemas.events` (Phase 22)
- Pydantic models (`extra="forbid"`) for every server event: `TranscriptEvent`, `SayEvent`, `BeliefEvent`, `EvalEvent`, `BlockedEvent`, `EscalateEvent`, `LatencyEvent`, `AuditEvent` (`private: bool`), `PhaseEvent`, `AgreementEvent`, `SttErrorEvent`, `ErrorEvent`, `TurnDoneEvent`, `TurnTraceEvent`, `AutoplayDoneEvent`; client: `StartEvent`, `EndEvent`, `TextEvent`, `SentenceDoneEvent`, `BargeInEvent`, `TimingEvent`
- `ServerEvent` / `ClientEvent` (discriminated on `type`); `SERVER_EVENT_ADAPTER`; `SERVER_EVENT_TYPES`; `View = Literal["rep","operator"]`, `VIEWS`
- `TurnTrace` — `turn`, `creditor_text`, `stance`, `ask_bp`, `ask_quote`, `terms: [TraceTerm{field,value,quote,verified,hedged}]`, `dropped: [DroppedTerm{field,value,reason,quote}]`, `belief_changes: [TraceBeliefChange]`, `affordability: {max_bp, curve: [{bp, feasible}] x100} | None` (operator only), `decide: {intent, reason, reason_key, reason_text}`, `counter_bp`, `nlg: {mode, source, template, guards: [{stage, ok, reason, offending}], fallback_used, fallback_reason}`, `spoken: [{id, text}]`, `timings`
- `export_schema() -> dict`, `schema_text() -> str`, `main(argv) -> int`; CLI `python -m app.schemas.events --out web/src/types/events.schema.json` (root properties `ServerEvent`, `ClientEvent`, `View`)

### `app.voice.views` (Phase 22)
- `redact_for_view(payload, view) -> dict | None` — every WS frame passes through it; operator: audit rows gain `private`; rep: drops `eval.max_bp/program_fee_cents/additional_funds`, schedule rows keep only `date` + `creditor_payment_cents` (eval and agreement), `blocked.offending`, `turn_trace.affordability`, guard `offending`, and private audit rows
- `is_private_audit(actor, event) -> bool` — actors `engine`, `agent`, `llm`; events `blocked`, `effects_committed`, `barge_in`, `nlg_template_rejected`, `llm_unavailable`

### `app.autoplay` (Phase 22)
- `DEFAULT_PAUSE_MS = 1200`, `MAX_PAUSE_MS = 10000`, `clamp_pause_ms(raw) -> int`
- `autoplay_settings(base) -> Settings` — `nlu_mode=oracle`, `nlg_mode=bank` if base is bank else `template`
- `load_autoplay_scenario(scenario_id, call) -> sim.scenarios.Scenario` — reads `fixtures/scenarios/<id>/sim.json` (`persona`, `opening_ask_bp`, `floor_bp`, `rules{…, first_payment_date: "default"|"after_last_draft"|ISO}`); `ValueError` if absent
- `new_autoplay_call(call, scenario_id, *, settings, audit, call_id=None) -> (Orchestrator, CreditorPolicy)` — orchestrator `llm=None`, `auto_ack=True`
- `async run_autoplay(orch, creditor, *, on_agent, on_creditor, pause_s=0.0, max_turns=None) -> AutoplayResult(outcome, phase, final_intent, turns)`; `outcome_of(phase, intent, *, has_agreement)` → `deal`|`no_deal`|`escalate`|`incomplete`

### `app.domain.scenario` (Phase 23b addition)
- `rep_card_suggestions(markdown) -> list[str]` — `- ` bullets under the rep card's `## Suggested replies` heading, in order. Every curated `fixtures/scenarios/*/rep_card.md` has that section (digit-free lines).

### `web/` (Phase 23b; see `web/README.md`)
- `npm run gen:types` → `web/src/types/events.ts` from `events.schema.json` (CI diff-checks it); hand-written aliases in `web/src/types/protocol.ts`.
- `useCall(makeSocket?, makeId?)` → `{events, status, callId, lastCallId, view, autoplay, start(scenarioId, {view, autoplay}), end(), sendText(text, source?), sendJson, sendWav, addLocal, subscribe}`; autoplay start sends `autoplay_pause_ms: 1200`.
- `useVoice(io, deps?, sttMode?)` over `VoiceEngine` (`web/src/lib/voice/engine.ts`); `VoiceIO = {sendJson, sendWav, sendRepText, currentTurn, onTtsOnset?}`.
- `reduceCall` accepts a client-local `{type:"tts_onset", turn, ms}` → `turn_trace.timings.tts_onset_ms`.

### `app.cli`
- `python -m app.cli [fixtures/demo]` — type as rep; auto-acks; prints lines, belief, timings, verdict

### `app.voice.stt`
- `async def transcribe(llm, wav_bytes, *, prompt=None) -> tuple[str, float]` — `(text, stt_ms)` via client `transcribe`

### `app.voice.ws`
- `configure(audit, llm, settings=None) -> None`
- WS `/ws/call/{call_id}` — client: `start{scenario}`, binary wav, `text`, `sentence_done{id}`, `barge_in{spoken_ids}`, `timing{turn,vad_end_to_first_audio_ms}`; optional `oracle` on `text` when `nlu_mode=oracle`
- Phase 21: `_CallConnection` — reader task → `asyncio.Queue`; every event except `start` runs as its own task (concurrent with NLU); `_send_lock` keeps one handler's frames together; an `Utterance` returned to several merged callers is emitted once (the others get a bare `turn_done`). `latency` keys: `stt_ms`, `nlu_ms`, `engine_ms`, `policy_ms`, `nlg_ms`, `queue_ms` (incl. STT wait), `server_total_ms`
- server: `transcript`, `say`, `belief`, `eval` (incl. `max_bp`), `blocked`, `escalate`, `latency`, `audit`, `phase`, `agreement`, `stt_error`, `error`, `turn_done`
- Phase 22: `/ws/call/{call_id}?view=rep|operator` (default `operator`; other values → `error` + close). New server events `turn_trace` (one per emitted utterance, after `latency`, before the audit tail; `timings` include `stt_ms` for voice) and `autoplay_done{outcome, phase, final_intent, turns}`. Autoplay start: `{"type":"start","scenario_id":"easy_deal","autoplay":true,"autoplay_pause_ms":1200}` (curated id only); while it runs `text` / WAV get `error` "autoplay is driving this call", `sentence_done` / `barge_in` are ignored, `end` cancels it and closes normally. Auto-ack calls emit `agreement` inside the wrap turn. NLU `LLMUnavailable` (text, WAV, post-ack drain) → audit `nlu/llm_unavailable` + `error` "NLU unavailable: …" + `turn_done`; socket stays open

### `app.main`
- `create_app(*, settings=None, llm=None, audit=None) -> FastAPI`
- `GET /healthz` → `{"status": "ok"}` (Phase 22, keep-warm)
- Phase 23b: `create_app(..., web_dist: Path | None = None)`; `WEB_DIST = <repo>/web/dist`. `GET /` and any non-API path → `web/dist/index.html` (`Cache-Control: no-cache`); dist-root files (favicon) as-is, no-cache; `/assets/*` → `public, max-age=31536000, immutable`; paths under `ws`, `scenarios`, `calls`, `metrics`, `healthz`, `assets` never fall back (404); no build → 503 with the build command. `GET /scenarios` rows add `suggested: list[str]` (rep card). `GET /calls/{id}/events?view=rep|operator` and `/calls/{id}/export?view=…` (default `operator`; `rep` drops `is_private_audit` rows; export adds `view`; other values → 400)
- `GET /metrics/summary` → `{stage: {p50,p95,n}}`; stages `stt_ms`, `nlu_ms`, `engine_ms`, `policy_ms`, `nlg_ms`, `queue_ms`, `server_total_ms`, `vad_end_to_first_audio_ms`
- `app = create_app()` for `uvicorn app.main:app`

### `app.llm.prompts`
- `PLACEHOLDER_MEANINGS: dict[str, str]`
- `nlu_messages(utterance, last_agent_line, pending_readback, *, ref=None) -> list[dict]` — `ref` adds a `Today's date:` line (`analyze` passes its `ref`)
- `nlg_messages(intent, placeholder_ids, last_rep_line, *, recent_turns=None) -> list[dict]` — Phase 24b: `recent_turns` (`(role, text)`, oldest first) → "Recent conversation:" block of the last `NLG_CONTEXT_TURNS = 3` turns; `format_recent_turns(turns)`; `act_messages(kind, placeholder_ids, *, talking_point=None)` (bank builder only). NLU prompt asks for `asks_question` / `question_topic`

### `app.store.audit`
- `class AuditLog` — `__init__(path)`; `append(call_id, actor, event_type, payload=None) -> int`; `for_call(call_id) -> list[dict]`; `close()`
- WAL mode; BEFORE UPDATE/DELETE triggers raise `append-only`

### `app.llm.client`
- `LLMUnavailable` — all routed targets exhausted
- `strip_json_fences(text) -> str`
- `class FakeLLM` — `enqueue(role, response)`; `chat_json` / `chat_text` / `transcribe` (per-role queue)
- `class LLMClient` — `__init__(settings=None, *, providers_path=None, http_clients=None, on_call=None, fake=None, skip_health_check=False)`
  - `async chat_json(role, messages, schema: type[BaseModel]) -> T`
  - `async chat_text(role, messages, max_tokens) -> str`
  - `async transcribe(wav_bytes, prompt=None) -> str`
  - `async aclose()`
- `make_client(settings=None, **kwargs) -> LLMClient | FakeLLM` — offline profile returns `FakeLLM`
- Roles: `nlu` | `nlg` | `sim` | `stt` | `agent` (Phase 24b; eval A/B arms; a profile without an `agent` route uses its `nlu` route). Routing from `config/providers.yaml` profiles (`demo`/`eval`/`local`/`offline`).
- Phase 24b: 429 cooldown = max(Retry-After, body delay: Groq "try again in XmYs" / Gemini `retryDelay`); a per-day quota without a Groq delay cools ≥ `DAILY_QUOTA_COOLDOWN_S = 3600`.
- `on_call` meta (one per finished attempt, success or failure): `{role, provider, model, latency_ms, prompt_tokens, completion_tokens, cache_hit, failover_from, error, queue_ms}`; `error` is `None` on success; `queue_ms` (Phase 21) = limiter wait + short-429 Retry-After sleeps.
- Phase 21: `class QueueWait` (`.ms`); `queue_wait_scope() -> ContextManager[QueueWait]` sums `queue_ms` of calls inside it. `@dataclass(frozen) RouteTarget(provider, model, params={}, timeout_s=None)` (`.spec`); `parse_route_entry(raw: str | Mapping) -> RouteTarget` (ValueError on unknown keys / bad timeout). Buckets keyed `(provider, model)`, burst `min(rpm, 5)`, refill `rpm/60`/s, start full. Limiter wait + request share the per-attempt timeout. `LLMClient.on_call` is a settable property (propagates to the offline FakeLLM it built).
- Per-request timeout `Settings.llm_timeout_<role>_s` (or the route's `timeout_s`); timeout / `APIConnectionError` / HTTP errors fail over; any other exception propagates (no failover).
- `config/providers.yaml` route entry (Phase 21): `provider/model` or `{target: provider/model, params: {...}, timeout_s: N}`; `params` go in the request body via `extra_body` and into the response-cache key (key unchanged when params are empty); `timeout_s` overrides the role timeout for that target.

### `app.llm.call_audit`
- `LLM_CALL_ID: ContextVar[str | None]` (name `"llm_call_id"`) — call id of the turn in progress
- `llm_call_scope(call_id) -> ContextManager[None]` — set by `Orchestrator` public turn methods (`start`, `on_creditor_text`, `on_sentence_done`, `on_barge_in`, `on_rep_end`), by `ws.py` around STT, and by `eval.run_eval.run_one_scenario` around the whole call (sim included)
- `audit_llm_calls(audit, *, then=None) -> OnCallHook` — appends `actor="llm"`, type `llm_call` / `llm_call_failed`, payload = meta; no row when `LLM_CALL_ID` is unset; chains `then`

## Deviations from PLAN.md (`docs/history/PLAN.md`)

- Ruff `extend-exclude = ["feasibility"]` so vendored engine stays untouched (UP035 on `shapes.py` otherwise).
- Synthetic fixtures under `fixtures/engine/{even_ok,rescue_gap,balloon_ok,tier_ok,gap_curve}` replace take-home `cases/`; engine tests re-pointed; rescue expected amounts match `rescue_gap` (lump 17500, incr 4375 × 5 drafts).
- `max_token_pays` has no static `default_factory`; BeliefState keeps it ASSUMED and copies `max_payments` when that field is observed (PLAN: default is max_payments / no limit).
- Domain unit helpers live in `app/domain/units.py` (PLAN/PHASES said `money.py`); covers money, pct, date, and count.
- `evaluate(..., *, assumed=)` optional kwarg so assumed field names can be recorded without re-passing belief.
- Money regex uses comma-required branch plus plain `\$\s?\d+` so `$2500` is not truncated to `$250` (PLAN's `(?:,\d{3})*` form).
- `rendered_guard` accepts optional `ref=` for date parsing; commitment regex allows conjugated verbs (`commits`, `guarantees`, …).
- Abbrev forms (`2.5k` / `10K`) extracted as money before bare numbers (not listed in PLAN order; needed for hidden-figure coverage).
- `ExtractedTerm.field` uses `min_payment_tiers` (registry name) not PLAN's `min_payment_tier`.
- `Intent` lives in `policy.py` (with `Action`); `nlg` imports it — avoids a policy↔nlg cycle.
- `decide` takes `belief` + `NegotiationState` (not a full session object); rescue check is a boolean `rescue_within_guardrail` so policy stays pure (no engine call).
- `settlement_ask_pct` interpreted as percent points (45.0 → 4500 bp).
- OpenRouter free slug: PLAN’s `openai/gpt-oss-120b:free` is gone; eval uses `cohere/north-mini-code:free`.
- Mistral never first in any profile (last-resort fallback only); Experiment keys often 429 with `limit-req-minute=0` until workspace/phone setup.
- NLU uses `chat_text` + local JSON parse/coerce (not `chat_json`) so field-keyed LLM shapes still validate; `coerce_analysis_payload` accepts `{max_payments: {value, quote, hedged}}`.
- NLU `max_tokens=800` (1200 since Phase 20) and NLG `max_tokens=400` (PLAN said 80 for NLG) because gpt-oss reasoning tokens consume the completion budget.
- `Action`/`Intent`/`Phase`/`Effect` and `TurnAnalysis`/`ExtractedTerm` live in `app.domain` so `sim/` never imports `app.agent` (agent modules re-export).
- Pressuring private-info turns are (2, 3) not PLAN's (3, 5) so short rescue/no_fix calls still escalate offline.
- Counter ladder treats "at max" as the highest feasible counter strictly below the ask (ceiling), not raw `max_bp`, so unreachable asks NO_DEAL instead of looping.
- `ExtractedTerm.value` allows `date` (needed for `first_payment_date` oracle/sim reveals); PLAN listed only int|str|dict.
- Deterministic `repair_stance` after NLU: Gemini often labels "Agreed" / schedule-accept lines as `info`.
- FPD field ask/readback copy avoids the number-word `first` so `template_guard` does not block ASK.
- WS framing adds server `turn_done` after each turn / ack / barge / timing batch so clients can drain without blocking (not named in PLAN §8 event list).

## Open issues

Refreshed 2026-10-07 (Phase 25). Older phase handoffs keep their own lists; this is the current set.

- Phase 24b A/B: arm C (ReAct) resumes 2026-10-08; then judges and `docs/eval/ab_20261007/summary.md`. README and `docs/DESIGN.md` hold `<!-- AB-PENDING -->` placeholders until then.
- Hosted demo: not re-verified after the 23b/25 changes (needs a deploy, user step). Keep-warm workflow ships with the schedule commented out; the owner enables it.
- Voice: no browser-measured end-of-speech → first-audio run; the 4.7 s p50 figure is an estimate.
- NLU: LLM-side filler false accepts (f23, f30) under the newer prompt (Phase 28 in progress); `wants_to_end` / `firm` precision and hostility recall still weak (`docs/eval/nlu_corpus.md`).
- Native tool calling for the eval ReAct arm (JSON tool-call protocol in use; deferred in Phase 26).
- Mistral free tier often 429s (`limit-req-minute=0`); routed last. Local Ollama NLU too slow for the gates.

## Code-review remediation (2026-10-01)

Fixed must-fix findings from the post-phase-9 review (tests first):

- NLU: word-boundary quotes; clear unverified ask; verify `readback_response`; tiers not auto-verified.
- Numbers: invalid named/ISO dates no longer crash `extract_tokens`.
- Policy: wrap only on `stance=accept` (after contradiction/tentative); `_confirm_key` includes all CreditorRules fields; `next_counter` returns `None` when no legal bp; honor `wants_to_end`.
- Orchestrator: post-NLU queue does not overwrite live pending; `last_eval`/`agreed_bp` commit on speech ack only; WRAP without agreement → END.
- LLM: cap short 429 retries (3) then failover.
- Eval: no vacuous `agreement_valid=1.0`; WRAP-sans-agreement invalid; thresholds gate `no_deal_correct`/`deal_rate_given_zopa`; oracle disposition overlay under live NLU; eval forces `llm_cache=False`; sim strips injected commitment phrasing and re-raises `LLMUnavailable`.
- Guards: block `we have a deal` / bare `deal` commitment variants.

#### Cheap template re-check (`eval_20261001_081835_s7`)

```
# Eval summary

- run_id: `eval_20261001_081835_s7`  seed=7  profile=`eval`  nlg=`template`  sim=`template`
- git: `2c6add72a0d79a7ae895e15c0af94c5e2d7b0209`
- model share: gemini/gemini-3.1-flash-lite=100.0%

| metric | value |
|---|---|
| n_completed / n_scenarios | 12/12 |
| skipped_quota | 0 |
| agreement_valid | 1 |
| deal_rate_given_zopa | 1 |
| no_deal_correct | 1 |
| escalation_correct | 1 |
| unverified_figures_spoken | 0 |
| sensitive_leaks | 0 |
| guard_blocks | 0 |
| rule_extraction_accuracy | 1 |
| false_known_rate | 0.000 |
| readback_count (mean) | 0.000 |
| turns_to_proposal (mean) | 2.667 |
| surplus_captured (mean) | 0.412 |

thresholds: PASS
```

Closed by remediation: WRAP-without-agreement silent success; full-LLM sim false `demands_commitment` (oracle overlay + phrase strip); vacuous agreement_valid.

## Phase handoffs

### Phase 0 (2026-09-30)
- Files: `pyproject.toml`, `.gitignore`, `.env.example`, `.venv/` (local), `feasibility/` (vendored), `app/` (+ empty subpackages), `sim/`, `eval/`, `config/`, `tests/{engine,unit,e2e}/`, `fixtures/engine/*`, `app/config.py`, `tests/engine/test_units.py`, `tests/engine/test_rescue.py`, `uv.lock`.
- Tests: 32 passed in `tests/engine`.
- Notes: no ASSIGNMENT.md or cases/ copied.

### Phase 1 (2026-10-01)
- Files: `app/domain/{units,fields,facts,belief,scenario}.py`, `fixtures/demo/{client,offer,firm}.json`, `tests/unit/{test_units,test_fields_facts,test_belief}.py`.
- Tests: 73 passed (engine + unit).
- Notes: demo fixture is synthetic (NorthPeak Collections); independent of `fixtures/engine`.

### Phase 2 (2026-10-01)
- Files: `app/adapter/{engine_adapter,validator}.py`, `fixtures/engine/gap_curve/*`, `tests/unit/{test_engine_adapter,test_validator}.py`.
- Tests: 106 passed.
- Timing: demo affordability median 8.3 ms (see Environment facts).
- Notes: `gap_curve` has interior infeasible band at 18–20% between feasible regions.

### Phase 3 (2026-10-01)
- Files: `app/agent/{numbers,guards}.py`, `tests/unit/{guard_adversarial.jsonl,test_guard_regression,test_numbers}.py`.
- Tests: 173 passed.
- Notes: 48-line adversarial corpus; regression covers template + rendered stages.

### Phase 4 (2026-10-01)
- Files: `app/store/audit.py`, `app/agent/{nlu_types,policy,nlg}.py`, `tests/unit/{test_audit,test_policy,test_nlg}.py`.
- Tests: 207 passed.
- Notes: Intent enum + templates; `render_action` guard pipeline with SAFE_FALLBACK; policy rules 1–10 covered in unit tests.

### Phase 5 (2026-10-01)
- Files: `config/providers.yaml`, `app/llm/client.py`, `tests/unit/test_llm_client.py`, `scripts/smoke_llm.py`.
- Tests: 216 passed (offline MockTransport: limiter, 429 short/long, Gemini 400→429, cache hit, missing key, LLMUnavailable, FakeLLM).
- Smoke (`scripts/smoke_llm.py`, 2026-10-01):
  - OK `groq/openai/gpt-oss-120b` 693 ms
  - FAIL `mistral/mistral-small-latest` 429 rate_limited
  - OK `gemini/gemini-3.1-flash-lite` 7616 ms
  - FAIL `openrouter/openai/gpt-oss-120b:free` 404 free slug unavailable
  - FAIL `cerebras/llama-3.3-70b` 404 model_not_found
  - SKIP `ollama/qwen3.5:9b` (model not pulled)

### Phase 6 (2026-10-01)
- Files: `app/llm/prompts.py`, `app/agent/nlu.py`, `app/agent/nlg.py` (LLM `speak_action`), `tests/unit/test_nlu_nlg_llm.py`, `tests/live/test_nlu_live.py`.
- Tests: 224 passed offline (+1 skipped live); FakeLLM covers hallucinated quote drop, hedged two-fifty `verified=False`, `$250`→25000 verified, invalid JSON retry, NLG digit/unknown-placeholder fallback.
- Live NLU (`DSA_LIVE=1`, demo profile, Groq): **15/15 (100%)** on the synthetic rep corpus.

### Phase 7 (2026-10-01)
- Files: `app/agent/session.py`, `app/agent/orchestrator.py`, `app/cli.py`, `tests/unit/test_orchestrator.py`.
- Tests: 229 passed offline (+1 skipped live). Covers barge-in re-offer, contradiction→CLARIFY, private refuse→escalate, full call→PROPOSE_WRAP + validator-clean agreement, timings.
- Manual CLI (`python -m app.cli fixtures/demo`, demo profile, live Groq). Short transcript to PROPOSE_WRAP:

```
agent: Good morning, this is Synthetic Debt Relief. … How may I assist you today?
rep:   Max eight payments, minimum one hundred dollars, even payments please.
agent: [ASK_SETTLEMENT] What percentage of the balance would you like to settle?
       belief: max_payments/min_payment_cents/payment_structure → KNOWN
rep:   We are looking for a forty five percent settlement.
agent: [CONFIRM_SCHEDULE] We can schedule 2 payments totaling $562.50, starting on March 31.
rep:   Yes I accept that payment schedule. Agreed.
agent: [PROPOSE_WRAP] (NLG fell back to SAFE_FALLBACK once; agreement still drafted)
engine: feasible=True shape=even offer=$562.50 settlement=45%
agreement: NorthPeak Collections bp=4500 offer_total=56250 pending_client_approval rows=2
phase: WRAP
```

### Phase 8 (2026-10-01)
- Files: `app/domain/{actions,nlu_types}.py`, `sim/{personas,scenarios,creditor}.py`, `tests/unit/test_scenarios.py`, `tests/e2e/test_text_call.py`; policy ceiling-stuck fix; agent re-exports for moved types.
- Tests: 242 passed offline (+1 skipped live). Generator deterministic + all strata; e2e 3 personas × 3 strata (oracle NLU, template NLG, template sim): deal→valid agreement, rescue→escalate, no_fix→END, pressuring→escalate, zero leaks.

### Phase 9 (2026-10-01)
- Files: `eval/{run_eval,metrics,thresholds}.py|yaml`, `tests/unit/test_metrics.py`; policy CONFIRM/COUNTER/CLARIFY stall fixes; NLU `repair_stance` + `LLMUnavailable` re-raise; `ExtractedTerm` date; FPD ask copy without number-word `first`.
- Tests: 255 passed offline (+1 skipped live).
- Root-cause fixes (tests first, not thresholds): identical CONFIRM spam; accept stance mislabel; clarify infinite loop; identical COUNTER without reject stance.
- Local profile: models pulled (`qwen3.5:9b`, `gemma4:e4b`) but full 12-scenario run aborted — NLU ~100–200 s/call; no local summary.

#### Run 1 — cheap template (`eval_20261001_011123_s7`)

```
# Eval summary

- run_id: `eval_20261001_011123_s7`  seed=7  profile=`eval`  nlg=`template`  sim=`template`
- git: `acc121f90f340687f4acc435cdd336f826be4742`
- model share: gemini/gemini-3.1-flash-lite=100.0%

| metric | value |
|---|---|
| n_completed / n_scenarios | 12/12 |
| skipped_quota | 0 |
| agreement_valid | 1 |
| deal_rate_given_zopa | 1 |
| no_deal_correct | 1 |
| escalation_correct | 1 |
| unverified_figures_spoken | 0 |
| sensitive_leaks | 0 |
| guard_blocks | 0 |
| rule_extraction_accuracy | 1 |
| false_known_rate | 0.000 |
| readback_count (mean) | 0.000 |
| turns_to_proposal (mean) | 2.667 |
| surplus_captured (mean) | 0.412 |

## Latency (ms)

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 0.139 | 5820.078 | 70 |
| policy_ms | 0.038 | 0.116 | 70 |
| nlg_ms | 0.117 | 0.224 | 70 |
| server_total_ms | 1.374 | 5846.373 | 70 |
```

thresholds: PASS

#### Run 2 — local (`eval_20261001_012534_s7`)

Finished after ~1.8 h. Ollama NLU (`qwen3.5:9b`) quality too weak for deals/escalation; thresholds fail. p95 NLU ~185 s.

```
# Eval summary

- run_id: `eval_20261001_012534_s7`  seed=7  profile=`local`  nlg=`template`  sim=`template`
- git: `9a98e1290aa0b5160512e4fc796fba7d20a37042`
- model share: ollama/qwen3.5:9b=100.0%

| metric | value |
|---|---|
| n_completed / n_scenarios | 12/12 |
| skipped_quota | 0 |
| agreement_valid | 1 |
| deal_rate_given_zopa | 0.000 |
| no_deal_correct | 1 |
| escalation_correct | 0.000 |
| unverified_figures_spoken | 0 |
| sensitive_leaks | 0 |
| guard_blocks | 0 |
| rule_extraction_accuracy | 0.000 |
| false_known_rate | n/a |
| readback_count (mean) | 0.000 |
| turns_to_proposal (mean) | n/a |
| surplus_captured (mean) | n/a |

## Latency (ms)

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 0.124 | 185257.287 | 312 |
| policy_ms | 0.010 | 0.992 | 312 |
| nlg_ms | 0.037 | 1.092 | 312 |
| server_total_ms | 0.456 | 185261.219 | 312 |
```

thresholds: FAIL (`escalation_correct=0`)

#### Run 3 — full LLM (`eval_20261001_011347_s7`, thresholds after clarify-cap fix + resume of s0007_007)

```
# Eval summary

- run_id: `eval_20261001_011347_s7`  seed=7  profile=`eval`  nlg=`llm`  sim=`llm`
- git: `acc121f90f340687f4acc435cdd336f826be4742`
- model share: gemini/gemini-3.1-flash-lite=100.0%

| metric | value |
|---|---|
| n_completed / n_scenarios | 12/12 |
| skipped_quota | 0 |
| agreement_valid | 1 |
| deal_rate_given_zopa | 0.667 |
| no_deal_correct | 0.000 |
| escalation_correct | 1 |
| unverified_figures_spoken | 0 |
| sensitive_leaks | 0 |
| guard_blocks | 0 |
| rule_extraction_accuracy | 0.889 |
| false_known_rate | 0.000 |
| readback_count (mean) | 0.167 |
| turns_to_proposal (mean) | 4 |
| surplus_captured (mean) | 0.274 |

## Latency (ms)

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 4988.758 | 7009.362 | 57 |
| policy_ms | 0.063 | 0.220 | 57 |
| nlg_ms | 4097.445 | 6265.581 | 57 |
| server_total_ms | 9528.143 | 12488.742 | 57 |
```

thresholds: PASS

### Phase 10 (2026-10-01)
- Files: `app/voice/{stt,ws,metrics_buf}.py`, `app/main.py`, `app/static/{index.html,app.js,mock_script.js}`, `fixtures/demo/rep_card.md`, `tests/unit/test_ws.py`; session `last_max_bp` + NLG `blocked_out` → `last_blocked`; audit SQLite `check_same_thread=False` + lock.
- UI: Creditor rep | Operator (firm) switch; mock replay; VAD CDN + 16 kHz WAV; speechSynthesis + sentence_done; barge-in toggle; PRIVATE max affordable on operator only.
- Tests: 281 passed offline (+1 skipped live). WS text protocol (start/text/say/sentence_done/barge_in/turn_done) + `/metrics/summary` shape + STT wrapper.
- Live demo-profile text WS call (same event path as voice UI; STT not in this smoke) reached `PROPOSE_WRAP`:

```
t1 ASK_SETTLEMENT  server_total≈11401 ms (nlu≈6094, nlg≈5283)
t2 CONFIRM_SCHEDULE feasible=True max_bp=10000  server_total≈24488 ms (nlu≈15925, nlg≈8561)
t3 PROPOSE_WRAP  server_total≈10370 ms (nlu≈4810, nlg≈5559)
```

`/metrics/summary` after that call (n=4 turns incl. opening):

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 5452 | 14450 | 4 |
| policy_ms | 0.08 | 0.13 | 4 |
| nlg_ms | 6256 | 8320 | 4 |
| server_total_ms | 10886 | 22525 | 4 |
| stt_ms / vad_end_to_first_audio_ms | null | null | 0 |

Offline oracle/template WS (FakeLLM): server_total p50≈0.9 ms, p95≈8.1 ms (n=4).

### Phase 10.1 (2026-10-01) — demo UX + non-price recovery
- **Why no-deal on Oct 31:** demo client's `last_draft_date` is 2026-10-15; FPD after that → `afford.max_bp is None` → was hard `NO_DEAL_WRAP`. Policy is intentionally deterministic; brittleness was missing non-price moves.
- Files: policy `COUNTER_TERMS` + `alt_first_payment_date`; `BeliefState.accept_alternative`; NLU bare-year reject + yes/no fast readback; `FieldSpec.label`; scenario catalog `fixtures/scenarios/*` + `rebase_to`; audit `list_calls`/`export_call`; HTTP `/scenarios`, `/calls*`; WS `scenario_id`, `stt_error`, `end`; rep chat UI + operator resize/audit formatting + browser STT fallback.
- Interfaces:
  - `Intent.COUNTER_TERMS`; `decide(..., alt_first_payment_date=)`; `NegotiationState.terms_countered` / `pending_terms_alt`
  - `find_alt_first_payment_date(scenario, rules, *, ask_bp, requested, already) -> date | None`
  - `BeliefState.accept_alternative(field, value, quote, turn)`
  - `load_scenario(path, *, rebase_to=None)`; `list_scenario_metas()`; `resolve_scenario_dir(id)`; `load_rep_card(id)`
  - `AuditLog.list_calls(limit)`; `AuditLog.export_call(call_id)`
  - `Orchestrator.on_rep_end() -> Utterance`
  - WS client: `start{scenario_id}`, `end`, binary STT failures → server `stt_error` (socket stays up)
  - HTTP: `GET /scenarios`, `/scenarios/{id}/rep_card`, `/calls`, `/calls/{id}/events`, `/calls/{id}/export`
- Tests: 287 passed offline (+1 skipped live).
- Open: Groq remains the only server STT route in `providers.yaml` demo profile; browser STT is the offline fallback.

### UI polish (2026-10-01) — rep chrome
- Files: `app/static/{index.html,app.js}`, `app/voice/ws.py` (docstring), `tests/unit/test_ws.py`.
- Rep view: scenario / mock / STT / download hidden (operator-only); barge-in always on (no checkbox); Start/End unified toggle in conversation header (+ operator mirror); mic icon in compose field.
- Playbook: `loadRepCard` always runs on boot (not gated on `/scenarios` catalog success); light markdown→HTML for headers/tables/lists.
- WS: regression test for client `end` → `on_rep_end` (no `unknown event: end`).
- Chat bubbles + negotiation metric boxes restyled (lighter asymmetric bubbles; content-sized metric stack).
- Tests: 288 passed offline (+1 skipped live).

### UI fixes (2026-10-01) — end/restart, mic, chat, NLG phrasing
- `unknown event: end` was a **stale uvicorn** (started before the `end` handler landed, no `--reload`). Run the demo server with `--reload`, outside the sandbox (sandboxed runs also caused the STT `Connection error`).
- Client: `ending` flag now cleared on server `error`, on socket close, and by a 3 s timeout; `connectAndStart` closes the previous socket and ignores events from replaced sockets, so Start works after End.
- Mic: `micGen` generation counter + `micOn` flips first; stop during VAD load tears down the late instance and its `MediaStream`; browser STT uses `abort()` with handlers detached.
- Rep chat: no intent/latency meta (operator transcript keeps them); consecutive sentences grouped per speaker; removed `pre-wrap` whitespace bug; server `role: "creditor"` now styled as the rep side.
- NLG: `TEMPLATE_ONLY_INTENTS = {ASK, ASK_SETTLEMENT, NO_DEAL_WRAP, ESCALATE}` in `app/agent/nlg.py` skip the LLM (their slots are full sentences; LLM wrapping produced "…provide the max_payments for What's the most…"). `ask_text` copy reworded to address the rep directly.
- Tests: 290 passed offline (+1 skipped live).

### Operator scenario brief (2026-10-01)
- `scenario_details(scenario_id, *, rebase_to=None, root=None) -> dict` in `app/domain/scenario.py`: meta, creditor balances, PRIVATE client finances (SDA balance, draft amount/day/window, upcoming drafts + deposits), firm fee (`program_fee_bp`, `program_fee_cents`, `bank_fee_cents`). Dates rebased like the live call. Rep card stays rep-view only.
- HTTP `GET /scenarios/{id}` (rebased to `date.today()`); 404 on unknown/invalid id.
- Operator view: "Scenario brief" panel (Creditor / Client PRIVATE / Firm fees cards) refreshes on scenario change and on load.
- Tests: 291 passed offline (+1 skipped live).

### Phase 11 (2026-10-01) — README and final eval
- Files: `README.md` (problem, architecture mermaid, how-to-run, eval/latency/guard tables, limitations, synthetic + unaffiliated notes).
- Final checks: `pytest` 291 passed / 1 skipped; `ruff check .` clean.
- Cheap eval `eval_20261001_134429_s7` (seed=7, profile=eval, nlg=template, sim=template, gemini-3.1-flash-lite 100%): thresholds PASS. Metrics match prior remediation run (agreement_valid / deal_rate_given_zopa / no_deal_correct / escalation_correct / rule_extraction_accuracy = 1; leaks/unverified/guard_blocks = 0).
- Latency (cloud cheap): nlu p50≈4815 ms / p95≈10079 ms (n=70). Local latency table in README from `eval_20261001_012534_s7` (Ollama NLU p95≈185 s; quality fail retained as limitation).
- All phases 0–11 marked done.

### Negotiation + revision fix (2026-10-02)

- **Why:** Live `easy_deal` call `12ce9281` escalated on `contradiction_unresolved` after "3 payments instead", and never countered affordable asks (confirmed at 70% immediately). Root causes: (1) policy confirmed any affordable ask; (2) `BeliefState.observe` equality branch left CONTRADICTED stuck when clarify repeated the new value; (3) post-CONFIRM term changes went through contradiction/CLARIFY instead of revision.
- Files: `app/domain/belief.py`, `app/domain/nlu_types.py`, `app/agent/{nlu,policy,orchestrator,nlg}.py`, `app/llm/prompts.py`, `app/config.py`, `sim/creditor.py`, unit/e2e tests.
- Interfaces:
  - `TurnAnalysis.firm: bool`
  - `VerifiedAnalysis.firm` / `revises_terms` (orchestrator-only); `repair_firm`, `repair_revises_terms`
  - `NegotiationState.confirmed_bp: int | None`; `Settings.close_gap_bp: int = 200`
  - `policy._negotiate_affordable` / `_counter_action`; CONFIRM reasons: `ask_within_offer`, `rep_firm`, `counters_exhausted`, `no_lower_counter`, `ladder_stalled`, `gap_small`, `terms_revised`
  - `Orchestrator._eval_bp(bp)`; schedule eval moved post-`decide` in `_enrich_action`
- Deviation from PLAN: "confirm at ask when affordable" replaced by counter ladder; affordable path never NO_DEALs on counters.
- Open: contradictory persona flipping during NEGOTIATE is treated as a revision (discovery-phase contradictions still CLARIFY).
- Tests: 318 passed offline (+1 skipped live); `ruff check .` clean.
- Cheap eval `eval_20261002_011825_s7` (seed=7, profile=eval, nlg=template, sim=template): thresholds PASS. `deal_rate_given_zopa` / `no_deal_correct` / `escalation_correct` / `agreement_valid` / `rule_extraction_accuracy` = 1; leaks/unverified/guard_blocks = 0. `surplus_captured` mean **0.689** (was 0.412); `turns_to_proposal` mean 3.667 (was 2.667).

### WRAP close prompt (2026-10-02)

- **Why:** After accept, agent hedged with `SAFE_FALLBACK` (LLM `PROPOSE_WRAP` hit commitment guard) and stayed in `WRAP` until an extra turn; user expected a clear “sent for approval / anything else?” then end or renegotiate.
- Files: `app/agent/{nlg,policy,orchestrator}.py`, `app/domain/actions.py`, `app/llm/prompts.py`, `tests/unit/test_policy.py`.
- Behavior:
  - `PROPOSE_WRAP` template: sent for client approval + ask if anything else before ending; added to `TEMPLATE_ONLY_INTENTS`.
  - `WRAP`: conclude → `CLOSE`; new ask/reject/counter/offer/terms → `clear_wrap` + reopen `CONFIRM`/`NEGOTIATE`; schedule detail stays in `WRAP`.
  - Effect `clear_wrap` clears `agreement` / `agreed_bp` / `last_eval`.
- Open: none.
- Tests: 321 passed offline (+1 skipped live); `ruff check .` clean.

### CONFIRM pct ack (2026-10-02)

- **Why:** After accepting a counter (e.g. 60%), agent jumped straight into schedule details without acknowledging the percentage.
- `CONFIRM_SCHEDULE` template leads with `{settlement_pct} works for us. …`; `settlement_pct` always required; orchestrator enrich keeps it in `required`; NLG prompt matches.
- Files: `app/agent/{nlg,policy,orchestrator}.py`, `app/llm/prompts.py`, `tests/unit/test_nlg.py`.
- Open: none.
- Tests: 322 passed offline (+1 skipped live); `ruff check .` clean.

### VAD sensitivity (2026-10-02)

- **Why:** Mic often missed utterances. `vad-web@0.0.22` defaults `positiveSpeechThreshold=0.5`; UI also passed unused `getStream` (API is `stream` in 0.0.22).
- Files: `app/static/app.js`.
- Settings: threshold 0.35/0.2, `redemptionFrames=16`, `preSpeechPadFrames=10`, `autoGainControl` on; pass `stream` explicitly.

### Accept/counter + mic release (2026-10-02)

- **Why:** Accepting "fine" after a 71% counter confirmed stale 63%; "we agreed at 71%" wrapped the wrong deal. Mic deaf for early turns then hot; tab mic stayed on after end; VAD→browser STT banner.
- Root causes: barge-in omitted in-flight sentence ids so `offer_counter` never committed; `"fine"` not in accept repair; CONFIRM wrap ignored a corrected %; AudioContext suspended after async VAD import; onnx `1.18.0` vs vad-web's `1.14.0`.
- Fixes:
  - Client barge includes `speakingIds`; server barge keeps `offer_counter`/`record_confirm` when any sentence heard.
  - Accept repair: fine/ok/okay/sure/alright; CONFIRM accept with different stated % re-confirms (no wrap).
  - Resume AudioContext; pin onnx 1.14.0; hard-stop mic tracks + browser STT on end.
- Files: `app/agent/{nlu,policy,orchestrator}.py`, `app/static/app.js`, unit tests.
- Tests: 326 passed offline (+1 skipped live); `ruff check .` clean.

### Min-payment ASK loop (2026-10-02)

- **Why:** Bare `"110"` while asking minimum payment re-asked forever.
- Cause: LLM often emits `min_payment_cents=110` (dollars, not cents); prior_range `(1000,100000)` rejects → empty terms → ASK again.
- Fix: bare digits → dollars-vs-cents `CLARIFY` (`cents_ambiguity_clarify_action`); resolve on "dollars"/"cents"/$amount; `$`/`dollars` cues still bind without clarify.
- Files: `app/agent/{nlu,policy,orchestrator}.py`, `app/domain/actions.py`, `app/llm/prompts.py`, unit tests.
- Tests: 334 passed offline (+1 skipped live); `ruff check .` clean.

### Double CONFIRM_SCHEDULE on accept (2026-10-02)

- **Why:** After accepting a counter ("cool"), agent asked for schedule confirm twice before wrap.
- Cause: `record_confirm` / `set_phase` waited for TTS `sentence_done`. Typed/voice "yes" mid-speech saw no `confirmed_bp` → policy re-emitted `CONFIRM_SCHEDULE`. Soft-retry also re-confirmed on accept.
- Fix:
  - Eager COUNTER/CONFIRM bookkeeping on emit (idempotent `offer_counter`).
  - Identical-key accept → `PROPOSE_WRAP` (not soft re-confirm).
  - Typed `sendText` barges in before send.
- Files: `app/agent/{orchestrator,policy}.py`, `app/static/app.js`, unit tests.
- Tests: 335 passed offline (+1 skipped live); `ruff check .` clean.

### Rep-card creditor account (2026-10-02)

- **Why:** Only `easy_deal/rep_card.md` had a Creditor account table; other scenarios showed settlement rules only in rep view.
- Fix: add Creditor / outstanding / original balance (from each `offer.json`) to `counter_ladder`, `no_space`, `balloon_structure`, `late_start_date`, `rescue_escalate` rep cards.
- Not a UI filter — content was missing from the markdown fixtures.

### Unreachable-ask term-alt loop (2026-10-03)

- **Why:** Live `balloon_structure` call re-offered 8% forever after creditor rejected and asked for 50%. Weak min alt ($50) unlocked only `max_bp=800`; term-alt search stopped once the curve was non-empty; min field blocked from a deeper cut ($30) that unlocks 50%+.
- Fix:
  - `_engine_context` hunts term alts when ask > `max_bp` / not feasible, not only when `max_bp is None`.
  - Min/max scanners take `baseline_max_bp` and only return improving alts; progressive min/max counters allowed (FPD still one try).
  - Policy prefers `COUNTER_TERMS` over ceiling re-offer when ask is above `max_bp`.
- Files: `app/agent/{orchestrator,policy}.py`, unit tests.
- Tests: 376 passed offline (+1 skipped live); `ruff check .` clean.

### 8% lock when a later start unlocks 77% (2026-10-04)

- **Why:** `balloon_structure` with 5 payments, $50 minimum, balloon, ask 80%. Current month ceiling is 8% ($56). December 31 ceiling is 77%, and 60% is feasible there. The agent offered 8%, treated "okay" on the later start as accepting that 8%, then re-read 8% until no-deal.
- Causes: FPD search ranked the closest non-empty curve, so the current month beat December whenever 80% fit nowhere; "cool" was not an accept; a term-alt yes confirmed the last price counter; a confirm below the outstanding ask stalled into assumed-field questions.
- Fix: when the ask fits on no date, pick the highest ceiling; `cool`/`yes`/`yeah`/`yep` repair to accept; `decide(..., accepted_term_alt=)` does not confirm a stale lower counter; a confirm still under the ask reopens the ladder when that ask is feasible.
- Files: `app/agent/{orchestrator,policy,nlu}.py`, unit tests.
- Tests: 382 passed offline (+1 skipped live); `ruff check .` clean.

### Remove Mock UI replay (2026-10-04)

- Dropped operator Mock checkbox, `?mock=1`, and `app/static/mock_script.js`. Live scenario/WS only.
- Follow-up: `Cache-Control: no-cache` on `/`, cache-bust `app.js`, default scenario to `easy_deal` when unset so Start still works without Mock.

### Skip useless max-payment +1 alts (2026-10-04)

- **Why:** Live call asked 4→5→6 when 5 only raised the ceiling and 6 was the first count that unlocked a real ladder (ask 80% still infeasible; settle path started at 6).
- Fix: `find_alt_max_payments` / `find_alt_min_payment_cents` match FPD ranking — lowest/highest ask-feasible first; else jump to best ceiling (least invasive among ties), not progressive +1 / −$10.
- Files: `app/agent/orchestrator.py`, `tests/unit/test_term_alts.py`.

### Phase 12 (2026-10-06) — Honest offline policy eval

- Files: `sim/creditor.py`, `eval/{run_eval,metrics}.py`, `eval/thresholds.yaml`, `config/providers.yaml`, `scripts/smoke_llm.py`, `tests/unit/{test_sim_creditor,test_metrics,test_eval_settings}.py`.
- New CLI flags: `--nlu oracle|llm`, `--no-oracle-overlay` (see `eval.run_eval` interface).
- New metric names: `<rate>_n` + `<rate>_ci95` for `agreement_valid`, `deal_rate_given_zopa`, `no_deal_correct`, `escalation_correct`, `rule_extraction_accuracy`, `false_known_rate`, `stuck_rate`; quality `counters_spoken_max`, `counters_spoken_mean`, `max_counters`, `identical_consecutive_agent_moves`, `turns_to_outcome`, `stuck_calls`. Renamed `rule_extraction_n` → `rule_extraction_accuracy_n`, `false_known_n` → `false_known_rate_n`.
- Gates: removed vacuous `guard_blocks: ">=0"` (metric still reported); added `counters_spoken_max: "<=max_counters"`.
- Sim: all 7 TrueRules fields revealed (core four at opening incl. first payment date as a spoken date; late three on ask / read-back / schedule read-back). `COUNTER_TERMS` accepted or rejected under hidden rules.
- Smoke (`scripts/smoke_llm.py`, 2026-10-06): OK groq/openai/gpt-oss-120b 522 ms; FAIL mistral 429; OK gemini-3.1-flash-lite 7365 ms; OK openrouter/cohere/north-mini-code:free 1750 ms; **OK cerebras/gpt-oss-120b 466 ms**; OK ollama/qwen3.5:9b 38476 ms. Cerebras added to the eval profile's `nlu` and `sim` routes, after gemini.
- Offline oracle eval `eval_20261005_225429_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`): **65 s** wall, no keys needed (also verified with all keys blanked). 100/100 completed. agreement_valid 1 (n=23, CI 0.857–1.000); deal_rate_given_zopa 1 (n=23, 0.857–1.000); no_deal_correct 1 (n=22, 0.851–1.000); escalation_correct 1 (n=55, 0.935–1.000); rule_extraction_accuracy 0.670 (n=700 = 7×100, 0.634–0.704); false_known_rate 0 (n=469); stuck_rate 0 (n=100); leaks/unverified/guard_blocks 0; counters_spoken_max 10; identical_consecutive_agent_moves 54; turns_to_outcome mean 6.11.
- Gate failures: **only `counters_spoken_max` (10 > max_counters 4)** — expected. 18/100 calls exceed the cap, all `no_fix` flexible (10) / contradictory (8), ending `max_counters` after 9–10 spoken COUNTERs; the same calls produce all 54 identical consecutive moves (re-offered ceiling counter). Left failing for Phase 14.
- rule_extraction_accuracy 0.670 is honest, not a bug: escalated / no-deal calls never reach a schedule read-back, so their 3 late fields stay ASSUMED (4/7); deal calls score 7/7.
- Deviations: eval scores rule extraction and agreement validity against `CreditorPolicy.agreed_rules` (true rules + accepted COUNTER_TERMS), not raw `true_rules`. Engine-private leak values exempt when spoken as a PUBLIC fact (a counter at the ceiling is not a leak).
- Cleanup: creditor ASK branch duplicate removed; CLARIFY reuses `_reveal_field`; schedule-reject branches merged into `_min_reject`; CONFIRM/SPEAK_SCHEDULE share one branch; runner per-utterance bookkeeping in one `_record`; threshold operator parsing table-driven; smoke script OpenRouter slug updated to the routed `cohere/north-mini-code:free`.
- Observed, not fixed: policy READ_BACK for tiers speaks `str([])` ("So I have [] for the payment tiers"); after the tiers read-back the agent re-sends an identical CONFIRM_SCHEDULE before wrapping (policy, Phase 14 territory).
- Tests: 406 passed offline (+1 skipped live); `ruff check .` clean.
### Phase 14 (2026-10-06) — `decide()` bugs, bounded extract, invariants

- Files: `app/agent/policy.py`, `app/agent/orchestrator.py`, `app/domain/actions.py`, `pyproject.toml` (`slow` marker), `README.md` (`MAX_COUNTERS` row), `tests/unit/test_policy.py`, `tests/e2e/test_policy_invariants.py` (new).
- Bug: ceiling path re-offered the ceiling counter until `rejects` hit `max_counters`, and the ladder steps before it did not count, so `s0007_009_no_fix_flexible` (seed 7, n=12; the id comes from the 12-scenario set, not n=100) spoke 10 COUNTERs, the last four identical at 62%. Fix in `_ladder_unreachable`: at most `max_counters` COUNTERs; the last allowed one jumps to the ceiling; once the ceiling is on the table, any non-accept → `NO_DEAL(max_counters)`. Regression: `test_regression_s0007_009_no_fix_flexible_counter_loop` + 3 unit tests.
- Helpers (all private, in `app.agent.policy`): `decide` is a 57-line dispatcher over `_decide_interruptions` → `_decide_clarify` → `_decide_confirm` → `_decide_discovery` → `_decide_negotiate`. `_decide_interruptions` calls `_decide_wrap` for WRAP/END/ESCALATE phases (same order as before). `_decide_negotiate` uses `_term_alt_action`, `_confirm_accepted_counter`, `_reconfirm_on_table`, `_negotiate_affordable(t, ask_bp, afford)`, `_ladder_unreachable(t, ask_bp, afford)`. Per-call inputs are carried in the frozen `_Turn` dataclass. Small builders: `_ask_field`, `_escalate`, `_propose_wrap`, `_confirm_required`, `_spoken_value`.
- Behaviour check for the extraction: per-turn (intent, reason, spoken text) traces over 500 invariant seeds + the n=100 seed-7 eval set were byte-identical before and after (`tmp/trace.py`, not committed). Every reason code is unchanged except `no_counter_below_ask`, which was removed because it could not be reached.
- Invariants: `tests/e2e/test_policy_invariants.py::test_policy_invariants_over_seeds` (`@pytest.mark.slow`) runs one scenario per seed (stratum × persona cycling, sub-seed from `Random(seed)`). It checks: COUNTER < ask, COUNTER ≤ `last_max_bp`, ≤ `max_counters` COUNTERs, no identical consecutive COUNTER, terminates within `max_turns`, and every WRAP agreement validates. `DSA_INVARIANT_SEEDS` sets the seed count (default 100, so plain `pytest -q` runs 100; CI runs `pytest -q -m "not slow"` and skips this sweep). **500 seeds: pass** (~7 min).
- Eval `eval_20261005_233242_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`): **thresholds PASS**. `counters_spoken_max` 4 (was 10), `identical_consecutive_agent_moves` 0 (was 54), `turns_to_outcome` mean 5.25 (was 6.11). agreement_valid / deal_rate_given_zopa / no_deal_correct / escalation_correct = 1 with the same n; rule_extraction_accuracy 0.670 (n=700); leaks / unverified / guard_blocks = 0.
- Deviations: "validates under true rules" uses `CreditorPolicy.agreed_rules` (true rules + accepted COUNTER_TERMS), the same choice the Phase 12 eval made. The invariant sweep uses per-seed `generate_one`, not `generate(500, seed)`.
- Cleanup: removed `NegotiationState.rejects`, the `inc_reject_at_max` effect and its orchestrator handler (dead after the fix; dropped the `rejects=` kwarg from 3 policy tests, whose assertions are unchanged). Removed the unreachable `no_counter_below_ask` branch (test: `test_next_counter_always_below_ask_and_within_max`). Merged the duplicate `afford is None` ASK_SETTLEMENT branch into the ask-unknown branch (test: `test_ask_known_but_rules_unbuildable_asks_settlement`). Merged the duplicated term-alt gate for the empty-curve and above-ceiling cases into `_term_alt_action` (test: `test_ask_above_ceiling_fpd_already_countered_ladders`). Dropped the unreachable enum/int fallbacks in `_fact_for_value`, since enums always go through text slots (test: `test_clarify_enum_field_uses_text_slots`). Removed the unused `max_counters` param of `_stall_after_confirm`. Collapsed 8 copies of the confirm `required` set, 2 PROPOSE_WRAP builders, 5 ESCALATE builders and 2 ASK builders into one helper each.
- Observed, not fixed: tiers READ_BACK still speaks `str([])`, so a template reads "[] for the payment tiers" (NLG/registry copy, outside the policy). `next_counter` can return an off-grid `c_prev` when the curve changes; `decide` never re-emits it (stall → jump / no-deal), but the helper contract is loose.
- Tests: 415 passed offline (+1 skipped live), invariants at 100 seeds included; `ruff check .` clean.

### Phase 15 (2026-10-06) — NLU and safety corpus, flag fixes

- Files: `tests/nlu_corpus.jsonl` (177 synthetic hand-labelled lines), `eval/nlu_corpus.py`, `docs/eval/{nlu_corpus.md,nlu_corpus_before.jsonl,nlu_corpus_after.jsonl}`, `tests/unit/{test_nlu_repairs,test_nlu_corpus}.py`; `app/agent/nlu.py`; two tests in `tests/unit/test_nlu_nlg_llm.py` rewritten for the OR rule.
- Interfaces:
  - CLI `python -m eval.nlu_corpus --label BEFORE|AFTER [--profile demo] [--concurrency 4]` (Phase 21: `--audit-db`, default `eval/results/nlu_corpus_audit.db`; `run_corpus(..., audit=None, llm=None)`; each line runs in `llm_call_scope("corpus:<id>")`) — `llm_cache=True`, cache at `eval/nlu_corpus_cache.db` (keyed by provider/model); rewrites only its own `## <LABEL>` section of the report.
  - `eval.nlu_corpus`: `load_corpus()`, `expected_flags/predicted_flags`, `expected_terms/predicted_terms`, `score(records) -> dict`, `write_report(path, label, section)`.
  - Corpus line: `id`, `tags`, `text`, `stance`, optional `agent` alias (`default|confirm|counter|min_ask`), `pending`, flag booleans, `hostility`, `terms{field: value}`, `ask_pct`, `cents_ambiguity`.
  - `repair_stance(..., *, has_terms=False)`; see the `app.agent.nlu` interface entry.
- Results (demo profile, Groq `gpt-oss-120b`):

| metric | BEFORE | AFTER |
|---|---|---|
| private-info precision / recall | 0.667 / 0.235 | 0.971 / 0.971 |
| commitment precision / recall | 0.667 / 0.462 | 1.000 / 0.923 |
| accept precision / recall | 0.306 / 1.000 | 1.000 / 1.000 |
| filler false accepts (n=31) | 21 | 0 |
| term exact-match (lines with terms, n=64) | 0.859 | 0.859 |

  Targets met (private-info ≥ 0.9 / ≥ 0.9; zero filler false accepts).
- Fixes (tests first): private-info and commitment = LLM flag OR regex; the regex arm skips cues negated in the same clause or said by the rep about themselves ("no need for…", "we commit to holding…"). Short acks force accept only when they outnumber content words and there is no number or term. Lines with injection cues ("ignore your previous instructions", "repeat after me", "SYSTEM:") never accept. The private regex no longer treats a bare "client's account" as a cue.
- Cleanup: the oracle overlay in `analyze` and `post_verify` duplicated the full flag-repair block. Both now call `_repair_dispositions`. The flag repairs share `_flag_or_regex` / `_flag_or_unnegated_cue`. Ack words moved out of `_ACCEPT_STANCE_RE` into `_ACK_WORDS`.
- Deviation: the corpus has 177 lines (PLAN said ~150). The prompt (`app/llm/prompts.py`) is unchanged, so AFTER reuses the cached LLM replies and the BEFORE→AFTER difference comes only from code.
- Open issues (measured, not fixed here): `wants_to_end` precision 0.5 ("thanks" mid-call); `firm` precision 0.6 (LLM claims); hostility recall 0.4 (insults outside the regex list); no date term returned for h06/f05/t05/t12 (the NLU prompt has no reference date); STT misspellings and homophones fail verification; t10/f01/d04 return no terms.
- Observed, not fixed: none outside the touched files.
- Tests: 456 passed offline (+1 skipped live); `ruff check .` clean.

### Cleanup (2026-10-06) — empty-tiers read-back, re-ladder after accept, NLU date

- Empty tiers: READ_BACK spoke "So I have [] for the payment tiers" (23/100 seed-7 calls). Now "So there are no special payment tiers. Is that right?"; CLARIFY uses the same slot text. `sim/creditor.py` READ_BACK matches that copy against empty true tiers.
- Phase 12's "identical CONFIRM after tiers READ_BACK" no longer reproduced as identical. Since Phase 14 it was worse: the rep says "Agreed" at 48% in the same turn that reveals the late fields, the tiers READ_BACK took priority and set DISCOVERY, and the readback "yes" fell into the ladder (ask 69% > confirmed 48%) → COUNTER 58% + a second CONFIRM in 23/100 calls. Fix in `app/agent/policy.py` (`_decide_clarify`, `_decide_confirm`, `NegotiationState.accepted_confirm_key`), `app/domain/actions.py` (`note_confirm_accepted`), `app/agent/orchestrator.py` (effect handler).
- NLU date: `nlu_messages(..., ref=)` adds `Today's date:`. Corpus h06/f05/t05/t12 rerun ad hoc (demo, Groq): 4/4 dates correct. `docs/eval/nlu_corpus.md` not regenerated (the prompt change misses the cache for every line).
- Eval `eval_20261006_000830_s7` (oracle/template, n=100, seed 7): thresholds PASS. `surplus_captured` 0.689 (was 0.557 on `eval_20261005_235945_s7`), `turns_to_outcome` 5.04 (was 5.48); validity / deal / no-deal / escalation rates unchanged at 1; 0 lines with `[]`; 0 CONFIRM → READ_BACK → COUNTER.
- Tests: `tests/unit/test_policy.py` (+5), `tests/unit/test_nlu_repairs.py` (+2), `tests/e2e/test_policy_invariants.py::test_regression_s0007_000_tiers_readback_after_accept`. 464 passed offline (+1 skipped live); `ruff check .` clean.
- Still open: non-empty tiers still read back as `str(list)` (digits in a text slot); not seen in the sim. → fixed in "min_payment_tiers end to end" below.

### min_payment_tiers end to end (2026-10-06)

- **Why:** a non-empty tier was broken at every step. The NLU prompt asked for `{"up_to_payments","min_cents"}` (wrong meaning), dicts went into belief unconverted, the READ_BACK put `str(dict)` in a text slot (the guard blocked it, so "Let me check that figure…" and the tier was never confirmed), and a confirmed dict would crash the engine (`for frm, m in tiers` unpacks dict keys). The sim never generated tiers, so the eval could not see it.
- Fix (tests first):
  - NLU: prompt schema `{"from_payment","min_cents"}` with an example; `coerce_tiers` → sorted `(from_payment, min_cents)`; malformed dropped + audited. No `up_to_payments` alias. "First N payments" phrasing → `tiers_ambiguous` → policy CLARIFY, escalate after two.
  - Speech: new `Fact` kind `ordinal` (`render_ordinal`); tier READ_BACK / CLARIFY built from per-tier PUBLIC ordinal + money facts ("So I have a minimum of $75 from the 4th payment on. Is that right?"). No dict/list text in slots.
  - Sim: deal-biased samples get 1–2 rising tiers (30%, `from` in 2..max_payments, each floor $25–$100 above the last) from an independent `Random(key)`; the rep reveals them in words; READ_BACK matching rebuilds tiers from the facts; CLARIFY `tiers_ambiguous` re-reveals tiers.
- Files: `app/domain/{units,facts,nlu_types}.py`, `app/agent/{nlu,policy}.py`, `app/llm/prompts.py`, `sim/{scenarios,creditor}.py`, `tests/unit/test_tiers.py` (new, 36), `tests/e2e/test_tiers_e2e.py` (new), `tests/unit/test_nlu_nlg_llm.py` (one test moved to the new schema).
- Eval `eval_20261006_003052_s7` (oracle/template, n=100, seed 7): thresholds PASS. 11/100 scenarios have tiers (4 with two); 8 reach a deal, 8/8 `agreement_valid`, 8/8 score 7/7 rule fields; no agent line with brackets. Overall rates unchanged (validity / deal / no-deal / escalation 1, `false_known_rate` 0, `guard_blocks` 0); `turns_to_outcome` 5.12, `surplus_captured` 0.689.
- Deviation: tiered deal samples can fail deal classification and resample, so seed-7 deal scenarios are not guaranteed identical to earlier runs; the Phase 14 and cleanup regressions still pass.
- Tests: 501 passed offline (+1 skipped live); `ruff check .` clean.
- Not done (by request): per-tier quote verification; tiers stay unverified → always read back. Live NLU on tier phrasing not measured (no corpus lines with tiers).

### Phase 13 (2026-10-06) — CI + frozen evidence

- Files: `.github/workflows/ci.yml`, `docs/eval/README.md`, `docs/eval/policy_eval_20261006/` (`summary.md` / `summary.json` / `run.json` + 5 transcripts), `README.md` (CI badge only), `render.yaml` (optional, mirrors hosted demo).
- CI on push/PR (no secrets): `uv sync --group dev` → `ruff check .` → `pytest -q -m "not slow"` → `python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7` (job fails on any threshold miss).
- Freeze `policy_eval_20261006` from `eval_20261006_004050_s7` (thresholds PASS; `counters_spoken_max` 4; identical consecutive moves 0). Transcripts: pressuring × deal / rescue / no_fix (`s0007_002`, `s0007_035`, `s0007_068`) plus former 10-counter cases `s0007_075_no_fix_flexible` (n=100) and `s0007_009_no_fix_flexible` (n=12 regression). Pack ≈12 KB.
- Red-path check: scratch branch with broken counter cap (re-offer ceiling) failed CI eval gate; branch deleted after verify.
- Deviations: none. `render.yaml` installs `uv` in `buildCommand` (Render image has no uv by default).
- Observed, not fixed: none in touched files.
- Tests: same suite; CI path skips `@pytest.mark.slow` invariants (run locally with `DSA_INVARIANT_SEEDS` as needed).

### Phase 19 (2026-10-06) — Results-first README

- Files: `README.md`, `docs/PROGRESS.md` (status 12–19).
- Results sit under the demo GIF: policy-eval rates with n and 95% CI from `docs/eval/policy_eval_20261006/summary.md` (`python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`); NLU AFTER precision/recall from `docs/eval/nlu_corpus.md` (`python -m eval.nlu_corpus --label AFTER`). No baseline table (Phase 16 not done). No live server-latency table (Phase 17 not done). No `app.replay` in Verify (Phase 18 not done).
- Claim corrections: audit log records belief / blocks / escalations / NLU / `decide()`, not LLM HTTP calls; `MAX_COUNTERS` described as enforced (ceiling ladder, last offer at ceiling); private-info = LLM flag OR un-negated regex, `rendered_guard` `boundary` as backstop; leak scan token-matches a fixed blocklist and exempts PUBLIC collisions, does not catch paraphrase / voice / every PRIVATE engine fact.
- Verify: `pytest -q`; the oracle eval command above.
- Limitations added: same-author simulator, synthetic corpus labels, open demo endpoints (synthetic data), voice e2e not yet measured. Architecture mermaid and GIF kept.
- Deviations: Phases 16–18 never merged, so the README says so instead of inventing tables. No new code.
- Open: voice 20-turn browser timing still unmeasured; 16–18 still on the roadmap.
- Tests: same suite; no new tests.

### Phase 20 (2026-10-07) — correctness and honesty fixes (REVIEW_PLAN F2, F4 wording, F5, F6, F10–F13, F16–F19)

- Files: `app/llm/{client,call_audit (new),prompts}.py`, `app/config.py`, `app/agent/{nlu,nlg,orchestrator}.py`, `app/{main,cli}.py`, `app/voice/ws.py`, `app/static/app.js`, `eval/run_eval.py`, `README.md`, `docs/{ROADMAP,PROGRESS}.md`; tests `tests/seed7.py` (new helper), `tests/unit/test_llm_audit.py` (new), `tests/unit/{test_llm_client,test_nlg,test_nlu_nlg_llm,test_ws,test_app_js_contracts,test_tiers}.py`, `tests/e2e/{test_tiers_e2e,test_policy_invariants}.py`.
- New Settings: `llm_timeout_nlu_s=6`, `llm_timeout_nlg_s=4`, `llm_timeout_stt_s=8`, `llm_timeout_sim_s=15`.
- ContextVar: `app.llm.call_audit.LLM_CALL_ID` (`ContextVar("llm_call_id")`), set via `llm_call_scope`.
- F2: each chat / STT request runs under `asyncio.timeout(t)` and the SDK `timeout=t`; timeout → `_TargetFailed` → next route. Test: hung MockTransport fails over in < 2 s with a 0.2 s timeout (chat and STT). Limiter wait is not inside the timeout (Phase 21).
- F5: `on_call` → `audit_llm_calls(AuditLog)` in `app.main` lifespan (injected clients get the hook chained and restored on shutdown), `app.cli`, `eval.run_eval.run_one_scenario` (per-scenario audit db, previous hook chained/restored). Failed attempts are rows too (`llm_call_failed`, with `error`). FakeLLM emits a failure meta when its queue is empty.
- F6: `_NLU_MAX_TOKENS = 1200`.
- F17: `speak_action` re-raises `LLMUnavailable` (orchestrator fallback is now reachable and audited); `_chat` / `transcribe` fail over only on `_TargetExhausted` / `_TargetFailed` (429, 5xx, other HTTP, `APIConnectionError` incl. SDK timeout, our timeout, empty `choices`). A `TypeError` from request building propagates (test).
- F16: barge-in reuses `_BOOKKEEPING_EFFECT_KINDS`; `_maybe_draft_agreement` single predicate; `_owned_http` removed; STT retry deduped into `_transcribe_once`; repeated meta dicts into `_emit_call` / `FakeLLM._take`. Behaviour unchanged (existing tests).
- F13: OPENING = "Hello, this is an automated agent calling on behalf of {firm_name} about a client's account with you. {opening_disclosure} What payment terms can you work with for a settlement?"; default `opening_disclosure` = "I am authorized to discuss settlement options for this account." (old one repeated "automated agent"). No sim code matched the old copy.
- F10–F12 (`app.js` only): tiers render as "No special tiers" / "$75 from the 4th payment" (joined by "and"); `money()` uses `"en-US"`; rep hero drops the intent metric and "Last agent intent"; rep terms table has no Status column (operator keeps chips and intents).
- F18: the three ~20 s tests were all spent in `generate(100, 7)` (~19.5 s), not in the calls. `tests/seed7.py` rebuilds exactly the needed slots (`slot(i)`, `tiered()`) via `generate_one` with the same sub-seeds and ids, so the tests run the **same scenarios** (not a smaller n; nothing marked slow). `test_seed7_slots_match_generate` (`@pytest.mark.slow`) asserts equality with the real `generate(100, 7)`. Fast suite: 78 s → **19.8 s** (`pytest -q -m "not slow"`, 513 passed).
- F4/F19: README says cancel-and-merge is orchestrator-only, wired live in Phase 21; README audit text now includes LLM calls; Phase 14 CI sentence fixed; `ws.py` docstring lists every server `type`; ROADMAP §0 marked as a dated snapshot.
- Oracle eval `eval_20261006_201909_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`): thresholds PASS; the metrics table is byte-identical to `docs/eval/policy_eval_20261006/summary.md`.
- Deviations: added `llm_timeout_sim_s` (sim role needed a bound; eval-only). `ws.py` changed beyond its docstring by one `with llm_call_scope(call_id)` around STT (needed for "every LLM call audited"; loop structure untouched). F18 solved by exact slot rebuild instead of smaller n / slow mark. Frozen transcripts in `docs/eval/policy_eval_20261006/` keep the old opening line (evidence, not regenerated). Calls with no call id in context (`eval.nlu_corpus`) are not audited.
- Open issues: 6 s NLU timeout is below the observed Gemini NLU p95 (~7 s, eval profile), so live eval runs may fail over more often; production NLU still uses `chat_text`, so JSON mode is never sent for NLU (F16 last bullet, not in this phase's task list); PROGRESS "Open issues" list and leftover `.gitkeep` files not refreshed (P25).
- Tests: 513 passed / 1 skipped under `-m "not slow"` (19.8 s); full `pytest -q` 516 passed / 1 skipped (88.7 s, 100 invariant seeds); `ruff check .` clean.


### Phase 24a (2026-10-07) — A/B harness, ReAct and LLM-only arms (REVIEW_PLAN §2(c))

- Files (new): `eval/agents/{__init__,protocol,base,react_agent,llm_only_agent,arm_metrics}.py`, `eval/agents/README.md` (handoff), `eval/judge_naturalness.py`, `tests/unit/{test_eval_agents,test_judge_naturalness}.py`, `tests/data/policy_arm_golden.json` (pre-24a runner output for seed-7 slots 1, 2, 35, 68, 75). Edited: `eval/run_eval.py` (`--agent`, per-call `agent` / `transcript` / `llm_calls_per_turn` / `turn_latency_ms`, `run.json["agent","arm_metrics"]`).
- Approved, eval-only rule break: `react` / `llm_only` choose moves and write numbers, and they see client financials and `max_bp`. Every module docstring says so. Nothing in `app/` imports `eval.agents`.
- Interfaces: see `eval.agents`, `eval.judge_naturalness` and `eval.run_eval` above.
- Oracle CI eval `eval_20261006_204146_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`, default `--agent policy`): thresholds PASS. The metrics table is byte-identical to the pre-change baseline `eval_20261006_203103_s7`, `summary.json` minus latency is equal, and all 100 per-scenario JSONs match on every pre-24a key. Golden test: `test_policy_agent_output_identical_to_pre_24a_runner`.
- Smoke on the eval profile (`--scenarios 2 --seed 7 --profile eval --nlg template --sim-phrasing template`, live NLU):
  - policy `eval_20261006_204446_s7`: 2/2 ok; 1.58 LLM calls/turn; p50 turn 14.9 s.
  - react `eval_20261006_204654_s7`: 1/2 ok (deal, valid). s001 was `skipped_quota` after every `nlu` route was exhausted (6 s timeouts plus 429s). 2.2 calls/turn, max 4; p50 30 s; 1 unverified figure.
  - llm_only `eval_20261006_205053_s7`: 2/2 ok (deal valid; no-deal on s001). 3.25 calls/turn: 1 decision call plus failed or timed-out NLU attempts. p50 30 s; 1 unverified figure.
  - Threshold misses at n=2 are empty denominators or the measured LLM-arm behaviour, so they do not indicate harness bugs.
- Deviations:
  - Agent role: calls route through role `nlu`. Adding `agent` needs `app/llm/client.py` (`Role`) and `app/config.py` (timeout) changes, which were out of scope.
  - JSON tool-call protocol, because the client has no native function calling.
  - Tools added beyond the plan's list so that every Action intent the sim needs is reachable: `ask`, `ask_settlement`, `read_back`, `clarify`, `propose_wrap`.
  - `say(text)` maps to `ASK_SETTLEMENT` (`reason="say"`), because the sim reacts to intents, not text.
  - Both LLM arms share the NLU front end. LLM-only is therefore "1 decision call + NLU", not literally 1 call when NLU is live.
  - A failed LLM-arm wrap stays in WRAP with no agreement and is scored invalid. The orchestrator moves to END instead.
  - The opening line is the policy's disclosed template for every arm.
- Open issues: agent steps run under the 6 s NLU timeout and share NLU quota, so live A/B runs skip and fail over often. `coerce_bp` reads values ≤ 100 as percent. Rate the 20 human pairs once real A/B runs exist (24b).
- Tests: `pytest -q` 534 passed / 1 skipped (incl. 100 invariant seeds); `ruff check .` clean.

### Phase 21 (2026-10-07) — latency: measure then cut (REVIEW_PLAN F1, F4, F14, F15)

- Files added: `scripts/{latency_probe,build_template_bank}.py`, `app/agent/nlg_bank.py`, `config/nlg_bank.json`, `docs/eval/latency_20261007.md` + `latency_20261007/*.json`, `docs/eval/nlu_corpus_{default,low}_effort.jsonl`, `tests/unit/{test_ws_concurrent,test_nlg_bank}.py`. Changed: `app/llm/client.py`, `app/voice/{ws,metrics_buf}.py`, `app/agent/{orchestrator,nlg}.py`, `app/config.py`, `app/static/app.js` (voice code only), `config/providers.yaml`, `eval/nlu_corpus.py`, `scripts/smoke_llm.py`, `.env.example`, `render.yaml`, `README.md`, `docs/eval/nlu_corpus.md`.
- New Settings: `nlg_bank_path="config/nlg_bank.json"`; `nlg_mode` accepts `bank` (default stays `llm` in code; `.env.example` and `render.yaml` set `NLG_MODE=bank`).
- New timing keys (turn timings, WS `latency`, `metrics_buf`, `/metrics/summary`): `engine_ms` (`_engine_context`), `queue_ms` (limiter wait + short-429 Retry-After sleeps, summed via `queue_wait_scope`; inside the other stages, not additive; WS adds STT's wait).
- providers.yaml route schema: `provider/model` or `{target: provider/model, params: {...}, timeout_s: N}` (see `app.llm.client` interface). Demo `nlu` stays plain default effort; eval `nlu` Gemini has `timeout_s: 20`.
- F1: buckets per `(provider, model)`, burst `min(rpm, 5)`, refill `rpm/60`/s. Tests: 3 acquires on a fresh rpm=25 bucket < 0.1 s; rpm=600 sustained rate; per-model independence.
- F4: WS reader task + queue, handlers as tasks. Tests (`test_ws_concurrent.py`, FakeLLM with slow NLU): a second `text` during NLU → one merged turn (NLU prompt has both fragments, one `latency`); a `barge_in` during NLU is applied before the turn's `say`. README cancel-and-merge sentence restored.
- NLG bank: 45 templates, 8 keys (COUNTER, 3× CONFIRM_SCHEDULE by payment-level count, CLARIFY, READ_BACK, REFUSE_COMMIT, REFUSE_PRIVATE). The builder also drops guard-clean but wrong templates ("N payments of {offer_total}"). Tests: every entry passes `template_guard`; bank mode makes zero LLM calls (unit and a whole offline call).
- Voice client: `redemptionFrames` 16→8; `vad_end_to_first_audio_ms` sent from `utter.onstart`; local "One moment." after 1.2 s with no `say` (never sent, not acked; 1.5 s echo guard); preferred voices list.
- Latency (demo, 20 turns each; `docs/eval/latency_20261007.md`): text server_total p50 4126→2369 ms; voice WAV→first `say` p50 5512→3704 ms; STT 681→207; NLG 1245–1440→0; limiter wait p50 ~2.2 s→0. p95 still about 8–9 s, now from Groq's 8K tokens-per-minute 429 Retry-After (visible as `queue_ms`), not from our limiter.
- Corpus gate: `reasoning_effort: low` FAILS (private-info recall 0.853, commitment 0.846, hostility 0.0 vs AFTER 0.971 / 0.923 / 0.400). Rows DEFAULT_EFFORT + LOW_EFFORT in `docs/eval/nlu_corpus.md`.
- Carry-over:
  - [20.1] Measured Gemini NLU p50 11.4 s / p95 17.5 s (n=15) and Groq NLU p95 about 2.5 s. Eval-profile Gemini NLU route gets `timeout_s: 20`; demo keeps 6 s (Groq p95 is well under it). Tests: `test_route_timeout_override_parses_and_validates`, `test_route_timeout_overrides_role_timeout`, `test_shipped_providers_yaml_parses`.
  - [20.2] Limiter wait and request share one per-attempt `asyncio.timeout`; the wait is reported as `queue_ms`. Test: `test_limiter_wait_bounded_by_role_timeout` (empty bucket → fail over in < 1 s at a 0.2 s timeout).
  - [20.4] Corpus lines are audited under `corpus:<id>`. Test: `test_corpus_lines_are_audited_per_line`.
- Deviations:
  - The corpus gate compares against AFTER as asked, and also adds a DEFAULT_EFFORT row: the NLU prompt changed after AFTER, so DEFAULT_EFFORT is the like-for-like baseline.
  - The default Groq org's `gpt-oss-120b` daily token cap ran out mid-phase. AFTER probes and the rest of the LOW_EFFORT run used `GROQ_API_KEY_2` / `_3` from the main checkout's `.env` (separate orgs), passed by env override. `GROQ_API_KEY_1` is the same org as the default key.
  - Short-429 Retry-After sleeps also count in `queue_ms`. Without that, the AFTER tail (Groq TPM) was invisible.
  - Bank keys were collected from offline sim calls, plus live-only keys.
  - A `--oracle` probe run was added (all stages live except NLU).
- Open issues: see the DEFERRED list in the phase-21 report. Main ones: Groq TPM (8K/min, about 7 NLU calls a minute) now sets p95; an NLU `LLMUnavailable` still closes the socket; short-429 sleeps are not bounded by the role timeout; the app reads only `GROQ_API_KEY` (no multi-key pool).
- Tests: `pytest -q` 541 passed / 1 skipped (incl. slow); `ruff check .` clean. Oracle eval `eval_20261006_212101_s7` (100 scenarios, seed 7): thresholds PASS, rates identical to the frozen pack.

### Phase 22 (2026-10-07) — decision trace, role-scoped streams, autoplay (REVIEW_PLAN §2(a), F7)

- Files (new): `app/schemas/{__init__,events}.py`, `app/agent/reasons.py`, `app/voice/views.py`, `app/autoplay.py`, `web/src/types/events.schema.json` (generated), `fixtures/scenarios/<all 6>/sim.json`, `tests/wsutil.py`, `tests/unit/{test_events_schema,test_turn_trace,test_reasons,test_ws_views,test_autoplay}.py`. Changed: `app/agent/{orchestrator,nlu,nlg}.py`, `app/voice/ws.py`, `app/main.py`, `sim/scenarios.py` (`scenario_from_truth`), `.github/workflows/ci.yml`, `fixtures/scenarios/{no_space,rescue_escalate}/rep_card.md`, `tests/unit/test_ws.py`.
- Interfaces: see `app.schemas.events`, `app.agent.reasons`, `app.voice.views`, `app.autoplay`, and the Phase 22 notes under `app.voice.ws`, `app.agent.orchestrator`, `app.agent.nlu`, `app.agent.nlg`, `app.main`, `sim.scenarios` above.
- WS events for 23b (all in `events.schema.json`; generate TS from root `ServerEvent` / `ClientEvent` / `View`):
  - `turn_trace` — once per agent turn (opening and rep-end close included), after `latency`: `turn`, `creditor_text`, `stance`, `ask_bp`, `ask_quote`, `terms[]`, `dropped[]`, `belief_changes[]`, `affordability{max_bp, curve[100]}` (operator only, key absent on rep), `decide{intent, reason, reason_key, reason_text}`, `counter_bp`, `nlg{mode, source, template, guards[], fallback_used, fallback_reason}`, `spoken[{id,text}]`, `timings`.
  - `autoplay_done{outcome: deal|no_deal|escalate|incomplete, phase, final_intent, turns}` — last frame of an autoplayed call.
  - `audit.private` (operator stream) marks rows the rep never receives.
  - View param: `/ws/call/{id}?view=rep|operator`, default `operator`.
  - Autoplay start payload: `{"type":"start","scenario_id":"<curated id>","autoplay":true,"autoplay_pause_ms":1200}` (0–10000, default 1200).
- CI: new step regenerates the schema and fails on `git diff`; `test_committed_schema_matches_models` catches it locally.
- Tests: rep-view privacy over the scripted easy_deal call and autoplay of easy_deal / no_space / rescue_escalate — every frame is scanned for every `session.private_blocklist` value plus `max_bp`, bank fee, program fee % and amount (the same scan finds hits on the operator stream). Every frame of a scripted and an autoplayed call validates against `SERVER_EVENT_ADAPTER`. Autoplay: easy_deal → WRAP + agreement valid under the sim's agreed rules; no_space → NO_DEAL (`infeasible`); rescue_escalate → ESCALATE (`out_of_guardrail`); no LLM call even with `nlu_mode=llm` / `nlg_mode=llm`; pause applied; text refused while running; `sim/` import closure has no `app.agent`. Reason coverage: every literal reason in `policy.py` / `orchestrator.py` has text (fast), and every (intent, reason) seen in the 100-seed oracle eval has text (`@slow`, 47 s).
- Carry-over:
  - [21.2] NLU `LLMUnavailable` no longer closes the socket: `_on_text`, `_on_wav` and the post-ack drain in `_on_sentence_done` catch it, audit `nlu/llm_unavailable`, send `error` + `turn_done`. Tests: `test_nlu_llm_unavailable_keeps_socket_open` (FakeLLM with an empty `nlu` queue; next turn succeeds after enqueue), `test_stt_then_nlu_llm_unavailable_keeps_socket_open`.
- Deviations:
  - The demo fixtures had no machine-readable creditor truth, so each curated scenario gets a `sim.json` (hidden rules, ask, floor, persona; dates relative to the rebased client). With the rep-card rules as written, no_space and rescue_escalate are dealable at the ask (no_space max_bp 71%, rescue_escalate ladders to NO_DEAL), so their minimum payment is raised in both `sim.json` and the rep card: no_space $80 → $350, rescue_escalate $30 → $110. A human playing the card now sees the outcome the card's title promises.
  - `turn_trace` adds fields the 23a hand-written TS did not have: `decide.reason_key`, `nlg.source`, `nlg.fallback_reason`, guard `offending` (operator only). `decide.reason` is nullable (OPENING / ASK_SETTLEMENT have no code).
  - `turn_trace` is emitted for every utterance, including the opening and the rep-end close (no creditor side: `creditor_text`/`stance` null, empty terms).
  - Rep stream audit filter is a deny-list by actor/event (`is_private_audit`), backed by the frame-scan test, rather than an allow-list.
  - New `autoplay_done` server event (not in the plan) so the UI knows when the sim is finished.
- Oracle eval `eval_20261006_220857_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`): thresholds PASS; metrics table byte-identical to `docs/eval/policy_eval_20261006/summary.md` (no policy change).
- Tests: `pytest -q` 593 passed / 1 skipped (177 s, incl. slow); fast suite 591 passed / 1 skipped; `ruff check .` clean.
- Open issues: see the DEFERRED lines in the phase-22 report.

### Phase 27 (2026-10-07) — key pool

- Files: `app/llm/client.py`, `app/config.py`, `config/providers.yaml`, `.env.example`, `docs/eval/latency_20261007.md` (+ `latency_20261007/keypool_text.json`). Tests: `tests/unit/test_llm_key_pool.py` (new, 22 tests), `tests/live/test_key_pool_live.py` (new, `@pytest.mark.live`), `tests/conftest.py` (new, registers `live`), `tests/unit/test_llm_client.py` (hermetic `api_key_pool={}`, 3-tuple limiter keys).
- Env var convention: for a provider with `key_env: NAME`, the pool is `NAME` (if set) then `NAME_1`, `NAME_2`, ... contiguous from 1 (stops at the first missing suffix; a blank value counts as present but is skipped), from env and `.env`, deduplicated by value, in that order. Works for any provider's `key_env`, declared Settings field or not. A single unsuffixed key behaves as before (label `<provider>#0`). Use keys from **separate orgs**: Groq/Gemini free-tier limits are per org.
- New Settings:
  - `api_key_pool: dict[str, SecretStr]`: every `*_KEY` / `*_KEY_<n>` var from env + `.env` (custom source `_ApiKeyPoolSource`; it respects `_env_file=None`). Passing `api_key_pool={}` makes tests hermetic. Because dict fields are deep-merged across sources, an explicit init value drops the source.
  - `llm_key_cooldown_s: float = 60.0`: 429 cooldown when there is no Retry-After.
  - `Settings.api_keys(key_env) -> list[tuple[int, str]]`: `(suffix, value)` pool.
  - The declared `*_api_key` fields are now `SecretStr | None` (they were plain `str`, which put keys in `repr(Settings)`).
- providers.yaml: new optional per-provider `tpm` (Groq `tpm: 8000`). Demo `nlu` = `[groq/openai/gpt-oss-120b, cerebras/gpt-oss-120b, gemini/gemini-3.1-flash-lite, mistral/mistral-small-latest]`.
- Bucket key: `(provider, key suffix, model)` for the rpm bucket (`LLMClient._limiters`), the TPM budget (`_tpm`, only when `tpm > 0`) and the cooldown (`_cooldown`, monotonic-until). TPM is charged at `_estimate_tokens(messages)` (chars/4 + 4 per message), reconciled to `prompt_tokens + completion_tokens` from usage, and refunded on a 429. Disabled state is per key (all models).
- Rotation: `_pick_key` uses a round-robin cursor per provider, skips disabled and cooling keys, takes the first key with zero limiter wait, else the key with the shortest wait. `LLMClient._keys: dict[str, list[_ApiKey]]`. `_openai[provider]` is kept and holds the first key's client (membership is still "provider has a key").
- Failure handling (`_on_pool`, shared by chat and STT):
  - 429 or Gemini quota-400: cool that key for that model, then retry the same target on the next live key straight away.
  - Every key cooling: wait out the soonest cooldown only if it is ≤ 20 s, ends before the deadline, and fewer than 3 such waits have happened. Otherwise `_TargetExhausted` → next route.
  - 401/403: the key is disabled for the process, with one `WARNING` log line naming the label. Every key disabled → `_TargetFailed`.
  - 5xx: backoff on the same key.
  - The whole loop runs under one `asyncio.timeout(route timeout_s or role timeout)`.
- on_call meta: every meta (and FakeLLM's) now has `key_id`: `"groq#2"`, `None` for cache hits and fake. One extra `llm_call_failed` row is written per key that failed before the call moved to a **different** key. Retrying the same key after a cooldown adds no row (the target's final row covers it). `_TargetExhausted` / `_TargetFailed` carry `.key_id`.
- Safety: error text is passed through `_redact` (key value → label), errors are raised `from None` (no chained provider exception), and the cache key is unchanged (no key input). Tests cover audit rows, metas, log output and the exception text when the provider body echoes the key.
- Carry-over:
  - [21.1] Per-key TPM budget plus rotation. Tests: `test_tpm_budget_prefers_key_with_tokens`, `test_tpm_budget_waits_when_empty`, `test_round_robin_across_keys`. Probe: text NLU p95 8773 → 2215 ms, `queue_ms` p95 6051 → 0 (`docs/eval/latency_20261007.md`, "AFTER key pool").
  - [21.3] One deadline per target covers cooldown waits, 5xx backoff and key switches. Tests: `test_deadline_covers_short_429_waits`, `test_deadline_covers_5xx_backoff_and_key_switches`, `test_hung_key_times_out_within_deadline`.
  - [21.4] Demo NLU fallback is now `cerebras/gpt-oss-120b` (same model as the Groq primary; Cerebras inference is fast, and the live call passed). Gemini moves to third. I chose Cerebras over "the same Groq model on the pool" because, once every Groq key is cooling, the same model on Groq would also be cooling. Test: `test_shipped_demo_nlu_fallback_fits_role_timeout`.
- Live sanity (`DSA_LIVE=1 uv run pytest -q -s tests/live/test_key_pool_live.py`, one tiny call per key): groq#1 ok, groq#2 ok, groq#3 ok, gemini#0 ok, gemini#2 ok, gemini#3 ok, cerebras#0 ok. `GEMINI_API_KEY_1` has the same value as the shell's `GEMINI_API_KEY`, so it was deduplicated into gemini#0.
- Oracle eval `eval_20261006_220646_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`): thresholds PASS.
- Deviations:
  - With every key cooling, a short cooldown that fits the deadline is still waited out before failing over. This keeps single-key behaviour (`test_429_short_retry_*`) and stays bounded.
  - A 429 with no Retry-After now cools for `llm_key_cooldown_s` (60 s). Before, the target was marked exhausted until UTC midnight.
  - STT 5xx now gets the same-key backoff (inside the STT deadline) instead of failing over at once.
- Open issues: see the DEFERRED lines in the phase-27 report (smoke script and eval settings do not see suffixed pools; Cerebras key on Render; daily-quota 429s without Retry-After are retried every 60 s).
- Tests: `pytest -q` 580 passed / 2 skipped (incl. slow); `ruff check .` clean.

### Phase 23b (2026-10-07) — web console wire-up and cutover (REVIEW_PLAN §2(a))

- Files (new): `web/scripts/gen-types.mjs`, `web/src/types/protocol.ts`, `web/src/hooks/{useCall,useVoice,useScenarios}.ts` (+ `useCall.test.ts`, `useVoice.test.ts`), `web/src/lib/voice/{engine,vad,speech,wav}.ts`, `web/src/components/ScenarioBrief.tsx`, `tests/unit/test_web_serving.py`, `docs/assets/console-{operator-autoplay,creditor-live}.jpg` (120 KB, 105 KB). Regenerated: `web/src/types/events.ts`, `web/src/fixtures/call_easy_deal.json`. Changed: `app/main.py`, `app/domain/scenario.py`, `fixtures/scenarios/*/rep_card.md`, `web/src/{App.tsx, components/{AppShell,Conversation,DecisionTrace}.tsx, lib/{callState,format,repView}.ts, fixtures/index.ts, index.css}`, `web/scripts/gen-fixture.mjs`, `web/vite.config.ts`, `web/package.json` (+ `json-schema-to-typescript`), `.github/workflows/ci.yml`, `render.yaml`, `README.md`, `web/README.md`, `tests/unit/{test_ws_views,test_events_schema}.py`. Removed: `app/static/` (`index.html`, `app.js`), `tests/unit/test_app_js_contracts.py`.
- Interfaces: see `app.main`, `app.domain.scenario` and `web/` above.
- The test_app_js_contracts assertions are now behaviour tests (`useVoice.test.ts`: TTS ack on error, benign cancel, stale handlers after barge, barge spoken ids, echo guard, contaminated clip, Loading/Listening/Transcribing cues, browser STT paused during TTS, pinned VAD settings, onstart timing once, local backchannel, preferred voices, WAV encoding; `useCall.test.ts`: the log download survives the end of a call; `callState.test.ts` and `creditorLens.test.tsx`: en-US money, tiers in the spoken style, no status chips or `[]` in the rep lens).
- CI: new `web` job on Node 24 (`npm ci` → `gen:types` + `git diff --exit-code` → typecheck → lint → test → build). Render: downloads Node v24.21.0, then `(cd web && npm ci && npm run build)`, then `uv sync`.
- Manual check (Chrome, local uvicorn on :8023, demo profile, 2026-10-07):
  - Autoplay on every scenario, each ending as its card says: balloon_structure deal (6 turns), counter_ladder deal (6), easy_deal deal (6), late_start_date deal (7), no_space no deal (4), rescue_escalate escalate (5). Screenshot: `docs/assets/console-operator-autoplay.jpg`.
  - Live text call on easy_deal in the creditor's eye (`?view=rep`, live LLM NLU, the 6 suggested replies clicked in order): every line got an agent reply, with no errors. It ended `NO_DEAL_WRAP`, because the scripted replies did not match the agent's read-back questions; this is not a wiring fault. The 133 WS frames the page received, captured by wrapping `WebSocket` in the page (the scripted equivalent of reading the DevTools WS panel), contain no `affordability` / `max_bp` / `program_fee_cents` / `bank_fee_cents` / `balance_cents` / `additional_funds` keys, no non-null `offending`, no private audit rows, and none of the scenario's fee or savings amounts. The page shows 9 lock panels. Screenshot: `docs/assets/console-creditor-live.jpg`. Automated Chrome refused `speechSynthesis` (no user gesture), so each line showed "Speech playback failed" and was still acked (F06 path).
  - Fixture mode (`?fixture=1`) replays to the drafted agreement on the served build.
  - Lighthouse accessibility (lighthouse 12, headless): 96 at first, with one finding (dark-theme primary button, white on `#3987e5` at 3.6:1). After setting the dark `--accent-fg` to near-black: **100**.
  - **Not done: a voice call.** Automated Chrome has no microphone input, and granting the mic permission prompt is the user's call. The voice path is covered by the ported unit tests only. To run it by hand: `npm run build` in `web/`, start uvicorn, Start call, then mic on (Server STT) and speak two turns.
- Carry-over:
  - [23a.1] The generated schema has `ask_bp`, `ask_quote`, `counter_bp`, `stance`, guards `{stage, ok, reason, offending}` and `AuditEvent.private`, so no adapter or server change was needed. `timings` is `dict[str, float|null]` with no `tts_onset_ms`, so the client measures it (`say` → `onstart`) and adds it through the local `tts_onset` event. Tests: `test_web_fixture_frames_match_the_protocol` (every fixture frame validates against `SERVER_EVENT_ADAPTER`) and the `tts_onset` tests in `useVoice.test.ts`.
  - [23a.2] `/scenarios` returns `suggested` from each rep card's new `## Suggested replies` section. The UI loads it, and the static catalog is now fixture-only. Tests: `test_scenarios_carry_rep_card_suggestions`, `test_rep_card_suggestions_parses_only_its_section`.
  - [23a.3] The Decision trace and State columns are lazy-loaded, so the main chunk is 130 kB and Recharts loads after first paint. The default warning limit was **not** restored: Recharts 3 alone is 545 kB, and splitting its deps out leaves that unchanged (tried). The limit stays at 600.
  - [23a.5] Tiers use the spoken style: `moneyShort` gives "$75 from the 4th payment" and keeps cents only when non-zero. Test: `callState.test.ts` tier case.
  - [22.1] `?view=rep` on `/calls/{id}/events` and `/export`; the UI's Download log uses the lens's view. Test: `test_rep_http_export_and_events_leak_no_private_value` (the operator export trips the scan and the rep export does not; 400 on an unknown view).
- Deviations:
  - vad-web still loads from jsDelivr at mic-on time (same pins as Phase 21), not from npm, so the bundle stays small and the pins are unchanged.
  - STT modes are `server` | `browser` (the old `auto` = server with fallback, which `server` now does).
  - The socket view is fixed per call. Changing the lens mid-call re-filters client-side; operator detail for a rep-view call is not recoverable, and a notice says so.
  - The fixture generator now follows the real protocol: `reason_key`, `nlg.source` / `fallback_reason`, guard `offending`, no `fallback` guard stage, latency `engine_ms` / `queue_ms`, audit privacy by `is_private_audit`, and `tts_onset` as a local frame.
  - Fixed during the manual check: a finished call (its socket stays open after `autoplay_done` / END) locked the scenario picker; duplicate React keys on repeated guard stages.
- Oracle eval `eval_20261006_224448_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`): thresholds PASS.
- Tests: `pytest -q` 615 passed / 2 skipped (incl. slow, 173 s); `ruff check .` clean; web: typecheck, lint, 46 Vitest tests, build all green.
- Open issues: see the DEFERRED lines in the phase-23b report.

### Phase 24b (WIP, paused 2026-10-07, quota; C resumes 2026-10-08) — H3 conversational NLG + A/B (REVIEW_PLAN §2(c))

Paused on the orchestrator's instruction (usage limit). Interface entries above already describe the new code.

**Done (code + tests):**
- Task 1 ack act: `Action.ack` (PUBLIC `ack_*` facts, `source="creditor"`), built by new `app/agent/acts.py` (`ack_facts` / `attach_acts`) only for terms that became KNOWN or changed this turn, creditor-said, never colliding with the private blocklist; skipped before READ_BACK / CLARIFY / refusals / endings. Rendered by `nlg.render_acts` as a leading sentence ("Got it, 8 payments at a $100 minimum."); same two guards; a failing act is dropped (audit `nlg/act_dropped`), never SAFE_FALLBACK.
- Task 2 ANSWER: `Intent.ANSWER` (attached act only), `AnswerAct`, `ANSWER_POINTS` (5 number-free talking points). NLU fields `asks_question` / `question_topic` (prompt, coercion, `repair_question`: never on a private ask; an "other" question mentioning terms/figures is dropped — seen live). Private asks still REFUSE_PRIVATE first.
- Task 3: NLG prompt gets the last 3 public turns (`Orchestrator._recent_public_turns`, drops unspoken lines and any line with a private figure). Bank: `scripts/build_template_bank.py --acts` merged 7 `ACK` + 5 `ANSWER:<topic>` entries into `config/nlg_bank.json` (demo profile, reviewed by hand; 3 awkward ACK variants removed).
- `Settings.nlg_h3` (default False) gates acts in the orchestrator; eval arm `--agent policy_h3`; `--nlg bank`; `--providers PATH`; new `eval/ab_report.py` (common-scenario table, adoption rule, transcripts) with tests.
- `web/src/types/events.schema.json` regenerated (only change: `ANSWER` in the Intent enum).
- Policy unchanged: `test_h3_moves_identical_to_plain_policy_on_seed7` (same intents / reasons / outcome with acts on). Oracle CI eval `eval_20261006_224236_s7` (default agent): thresholds PASS, metrics table identical to `docs/eval/policy_eval_20261006/summary.md`.
- Carry-over (all done, each with a test):
  - [24a.1] client role `agent`, `Settings.llm_timeout_agent_s=20`, eval-profile `agent` route (groq → cerebras → gemini), `AGENT_ROLE="agent"` (`test_agent_role_timeout_and_nlu_fallback_route`, `test_shipped_eval_profile_has_free_tier_agent_route`, `test_agent_steps_use_dedicated_agent_role`).
  - [24a.3] not code: the "sim reacts to intents, not text" limitation must be stated in the A/B summary (still to write).
  - [24a.4] prompts ask for `"N%"`; **bug fixed**: `coerce_bp("1%")` returned 10000 (re-scaled after percent parse) (`test_coerce_bp_percent_form_is_prompted_and_unambiguous`).
  - [21.7] `NLG_MAX_TOKENS=800`, `reasoning_effort: low` on Groq nlg routes; an empty LLM template falls back to `TEMPLATES` (`test_empty_llm_template_falls_back_to_default_template`, `test_shipped_groq_nlg_routes_use_low_reasoning_effort`).
  - [22.5] `counter_no_total` display text (`test_counter_without_offer_total_omits_the_amount`).
  - [27.2] `_build_settings` passes `api_key_pool` (+ cooldown, bank path, nlg_h3, timeouts) (`test_build_settings_keeps_explicit_key_pool`).
  - [27.4] 429 cooldown parsed from body (Groq "try again in", Gemini `retryDelay`), per-day quota ≥ 3600 s (`test_quota_429_cooldown_from_error_body`, `test_daily_quota_moves_on_instead_of_retrying_each_minute`).

**User decisions (2026-10-07, via orchestrator):**
1. Arm D (`llm_only`) is **dropped** at its partial 13/48 (REVIEW_PLAN cut order allows it). Do not resume it; report it as partial in the summary.
2. Arm C (`react`) resumes **2026-10-08** after the daily quota reset, to 48/48; then the three judges and `summary.md`.
3. The pre-registered adoption rule stays **exactly as written** (no restating relative to A). The summary says B fails as registered and notes that A also misses `agreement_valid = 1` under live NLU.
4. The orchestrator merges the current code (through this commit) into main on 2026-10-07; work continues on `phase-24b` in this worktree.

**Left (2026-10-08):**
- Task 4: resume C to 48/48 (one process; command below), then judges B vs A, C vs A, C vs B.
- Task 5: `docs/eval/ab_20261007/summary.md` via `eval.ab_report` with arms A, B, C (D left out of the common-scenario table so it does not shrink n to its 13; give D's partial numbers in a separate labelled paragraph, e.g. from a separate `ab_report --arm A=... --arm D=...` run over their common scenarios). Notes preamble `docs/eval/ab_20261007/notes.md` + a decision paragraph: B fails the rule as registered (leaks, `agreement_valid`; naturalness from the judge), A also misses `agreement_valid = 1` under live NLU, D dropped as partial; keep the [24a.3] limitation note.
- Task 6: decided — **A stays the demo default** (B fails the rule; `Settings.nlg_h3` already defaults to False, no config change). Record it in the summary and the handoff.
- Final: full `pytest -q`, `ruff check .`, oracle CI eval, replace this WIP section with the final Phase 24b handoff (Action fields, `Intent.ANSWER`, NLU fields, A/B commands, carry-overs incl. the first-payment-date fix), commit `phase 24b: <summary>`.

**A/B state at second pause (2026-10-07 16:50 IST; results are git-ignored, they live only in this worktree under `eval/results/`):**
- A `ab1007_A_policy`: **48/48 ok**. B `ab1007_B_policy_h3`: **48/48 ok**.
- C `ab1007_C_react`: 9 ok, 4 `skipped_quota` (13 attempted). D `ab1007_D_llm_only`: 13 ok, 3 `skipped_quota` (16 attempted). Both stopped by hand: Gemini free tier hit its **per-day** cap (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `retryDelay` 45671 s ≈ 12.7 h from 16:49 IST), Groq's daily token cap was already spent, and Cerebras / OpenRouter time out under load, Mistral 429s, so every scenario was skipping. `--resume` re-runs `skipped_quota` / `error` files.
- Fixed during this resume: `Orchestrator._engine_first_payment_date()` — a denied first-payment-date read-back leaves the field UNKNOWN (`value=None`), `build_rules` does not require it, and the old `assert isinstance(fpd, date)` (3 sites: `_eval_bp`, `_engine_context`, wrap validation) crashed the turn (A/B `s0007_025/028/037`, contradictory persona, both arms). Now falls back to the engine's EOM default (the same value the belief starts with as ASSUMED), audited `engine/first_payment_date_default`. Test `test_denied_first_payment_readback_falls_back_to_engine_default`. Oracle CI eval after the fix: PASS, metrics table identical to `docs/eval/policy_eval_20261006/summary.md`.
- Preliminary A vs B (offline `eval.ab_report`, 48 common scenarios): B fails the pre-registered rule on leaks (1: `s0007_006`, a **policy** ACCEPT echoing an LLM-sim "100% balance" that equals the true ceiling; no H3 act spoke a private figure) and on `agreement_valid` (0.71; A is 0.75 too: live-NLU extraction errors, e.g. `s0007_015` fails identically in both). So the demo default stays A whatever the judges say. Draft preamble: `docs/eval/ab_20261007/notes.md` (includes the [24a.3] limitation).

**Resume commands** (2026-10-08; C only, a single process, so the full-rate providers file would also do; keep `providers_split2.yaml` for identical conditions):
```bash
P="--scenarios 48 --seed 7 --profile eval --nlu llm --sim-phrasing llm --no-oracle-overlay --providers docs/eval/ab_20261007/providers_split2.yaml"
# A and B are complete (48/48); D is dropped (13/48, do not resume).
uv run python -m eval.run_eval $=P --agent react --nlg template --resume ab1007_C_react
# judges (RUN_A = the arm being rated), after C finishes:
uv run python -m eval.judge_naturalness eval/results/ab1007_B_policy_h3 eval/results/ab1007_A_policy --profile eval
uv run python -m eval.judge_naturalness eval/results/ab1007_C_react eval/results/ab1007_A_policy --profile eval
uv run python -m eval.judge_naturalness eval/results/ab1007_C_react eval/results/ab1007_B_policy_h3 --profile eval
# report (A, B, C; D reported separately as partial):
uv run python -m eval.ab_report --arm A=eval/results/ab1007_A_policy --arm B=eval/results/ab1007_B_policy_h3 \
  --arm C=eval/results/ab1007_C_react \
  --judge B:A=<judge dir> --judge C:A=<judge dir> --judge C:B=<judge dir> \
  --out docs/eval/ab_20261007 --decision <decision.md> --notes docs/eval/ab_20261007/notes.md
```
(`$=P` is zsh word-splitting; in bash use `$P`. A plain `$P` in zsh passes one argument and argparse rejects it.)

**Gotchas:**
- Never run more than two arms at once: each process paces its own keys, so 4 processes oversubscribed Gemini (15 rpm/key real limit) and cascaded to `skipped_quota`. Two processes need `providers_split2.yaml` (all rpm/tpm halved).
- `generate(24, 7)` is **not** a prefix of `generate(48, 7)`; if quota forces fewer scenarios, keep n=48 with `--resume` and let `ab_report` compare the scenarios every arm completed (it does that by design).
- `GROQ_API_KEY_1`'s org hit its daily token cap (TPD 200k) during the aborted 4-way run; the react arm's `agent` route starts on Groq, so it will lean on Cerebras/Gemini until that resets.
- Live NLU sometimes reads a stray number as the settlement ask (seen: "10%" from "10 payments" context), and LLM sim phrasing loops on restated amounts; both affect every arm equally (not 24b scope).

**Checks at second pause:** `uv run ruff check .` clean; full `uv run pytest -q` 672 passed / 2 skipped; oracle CI eval PASS (`eval_20261007_104338_s7`).

### Phase 26 (carry-over) (2026-10-07) — carry-over cleanup

- Files added: `tests/unit/test_smoke_llm.py`, `web/src/App.test.tsx`. Changed: `app/llm/client.py`, `app/agent/{nlu,orchestrator}.py`, `app/voice/views.py`, `scripts/smoke_llm.py`, `config/providers.yaml`, `fixtures/scenarios/*/rep_card.md`, `tests/nlu_corpus.jsonl` (+6 lines `k01`–`k06`), `docs/eval/ab_20261007/providers_split2.yaml` (Cerebras `tpm: 15000`, see deviations), tests.
- Interfaces:
  - `LLMClient.chat_text(role, messages, max_tokens, *, json_mode: bool = False) -> str` (same on `FakeLLM`, which ignores it). `json_mode=True` sends `response_format: json_object` on providers with `json_mode: true`, appends the JSON-only system line elsewhere, and returns raw text (the caller parses). NLU (`app.agent.nlu.analyze`) now calls it with `json_mode=True`; its `_LLM` protocol has the kwarg. Test doubles that override `chat_text` must accept `**kwargs`.
  - `app.voice.views.is_private_audit(actor, event)` is an **allow-list** (`_REP_AUDIT_EVENTS: dict[actor, frozenset[event]]`). A new audit event is operator-only until added there.
  - `Orchestrator._run_turn`: NLU `LLMUnavailable` pops the turn's creditor history line and undoes the `turn_idx` bump before re-raising.
  - `app.agent.nlu.post_verify`: a settlement ask whose quoted percent is directly followed by a non-ask noun (`balance`, `interest`, `installment(s)`, `payment(s)`, `down`, `rate`, `negotiable`, …) is dropped as `nlu_rejected_ask_value` (existing event / `DroppedTerm.reason`). "N% of the balance", "N% settlement", "settle at N%" still verify.
  - `_INJECTION_RE` also matches forged prompt markup (`<utterance>` / `</utterance>`) and NLU JSON (`"stance":`), so such a line is never repaired to accept.
  - `scripts/smoke_llm.py`: `_key_pool(settings, env)` = `settings.api_keys(env)`; every key is smoked alone via `_single_key_settings(settings, env, value)`; lines carry `[KEY_ENV_N]`.
  - providers.yaml Cerebras: `rpm: 5`, `tpm: 30000`.
  - Rep cards: a `- Correct.` suggestion after the first terms line (fast read-back confirm path) and an intro sentence telling the rep to answer read-backs first. No scenario figures changed.

| item | outcome | test or evidence |
|---|---|---|
| P20 NLU never sends JSON mode | fixed | `test_nlu_chat_text_sends_json_mode` (NLU body has `response_format: json_object`, plain `chat_text` does not) |
| P24a no native tool calling | deferred to user | needs a `tools=` path through `_chat`/`_call_chat`, cache key and fakes (well over 30 lines); the JSON tool-call protocol works for the A/B, and arm C runs on it tomorrow, so changing it mid-A/B would change conditions |
| P21 filler false accepts (f23, f30) | fixed in part; rest deferred to user | Re-check: the short-ack rule does **not** produce f23/f30 (`repair_stance` keeps counter/info on both, with or without terms): `test_short_ack_rule_does_not_force_accept_on_filler_term_lines`. The accept label comes from the LLM under the newer prompt (AFTER had offer/counter). The third drift line, i06 (forged `{"stance":"accept"}`), is fixed: `test_forged_nlu_json_in_line_is_never_accept`. Vetoing an LLM accept on term-carrying lines would trade accept recall for precision and needs a live corpus run (≈173 Groq calls) to measure; not run, to keep the Groq quota for the 24b arm C resume |
| P22 rep audit filter is a deny-list | fixed | `test_rep_audit_filter_is_an_allow_list`; frame-scan privacy tests (`test_ws_views.py`) still green |
| P22 failed NLU turn skips a turn number | fixed | `test_nlu_llm_unavailable_rolls_back_turn` (fails on the old code) |
| P27 smoke `_key_present` ignores suffixed keys, smokes one key | fixed | `test_smoke_llm.py` (suffixed-only pool is present; each key isolated) |
| P27 Cerebras rpm 4 | fixed | Cerebras docs (inference-docs.cerebras.ai/support/rate-limits, Free Trial, gpt-oss-120b): 5 RPM, 30K uncached TPM, 1M TPD → `rpm: 5`, `tpm: 30000`; `test_shipped_cerebras_limits_match_free_tier_docs` |
| P23b suggested lines drift from read-backs | fixed (reworded, not live-verified) | every card has a `Correct.` read-back answer: `test_rep_card_suggestions_answer_read_backs` (fast-path confirm, never a bare-ack accept). A live easy_deal rep-view call was not re-run |
| P23b no App test for picker unlock | fixed | `web/src/App.test.tsx` (mocked fetch + WebSocket; locked mid-autoplay, unlocked after `autoplay_done`; fails with the 23b fix reverted) |
| P24b "100% balance" / "10% balance" read as the ask | fixed | `test_percent_naming_a_non_ask_is_not_the_settlement_ask` (6 shapes from the A/B transcripts), `test_real_asks_still_verify` (5 real-ask shapes); corpus `k01`–`k06` (`ask_neg`). Policy, reason codes and the cascade unchanged |

- Deviations:
  - P24b: the item suggested "require an explicit ask cue (verb + %)". That would drop real verbless asks already in the corpus (f26 "okay sixty percent then", f01 "forty uh fifty percent"), so the check is the reverse: reject the specific non-ask shape (percent bound to a non-ask noun), keep everything else.
  - `docs/eval/ab_20261007/providers_split2.yaml` gained Cerebras `tpm: 15000` because `test_split_providers_file_halves_every_rate` requires it to mirror providers.yaml halved. Its Cerebras rpm stays 2 (5//2 = 4//2), and 15K TPM cannot bind at 2 rpm, so arm C's conditions match A/B.
  - P20 also changes non-`json_mode` providers (Mistral, OpenRouter): NLU now gets the extra "Reply with JSON only" system line there. Cache keys are unchanged (they hash the caller's messages).
- Open issues: the P21 LLM-side false accepts (needs a live corpus run); P23b live verification of the reworded cards; P24a tools path.
- Checks: `uv run ruff check .` clean; `uv run pytest -q` 692 passed / 2 skipped; oracle eval `eval_20261007_145210_s7` thresholds PASS, metrics table identical to `docs/eval/policy_eval_20261006/summary.md`; web: typecheck, lint, 47 Vitest tests, build green.

### Phase 25 (2026-10-07) — recruiter packaging (REVIEW_PLAN F3, F9, F19)

- Files added: `docs/DESIGN.md` (5 ADRs, each ≤ 200 words, + an annotated voice-turn `sequenceDiagram` with the P21 AFTER voice p50s), `docs/assets/demo.mp4` (0.6 MB), `.github/workflows/keepwarm.yml`, `docs/eval/policy_eval_20261006/NOTE.md`, `tests/unit/test_docs.py`. Moved: `docs/{PLAN,PHASES,ROADMAP}.md` → `docs/history/`. Replaced: `docs/assets/demo.gif` (29 MB, 2026-10-02 UI → 1.9 MB, 1200 px, 12 fps, palette). Removed: `config/.gitkeep`, `eval/.gitkeep`. Changed: `README.md`, `docs/eval/README.md`, `docs/assets/README.md`, `CLAUDE.md` (paths only), `docs/PROGRESS.md` (status 20–28, Open issues).
- README: hero GIF (links the MP4), keyless "Watch a call" note, then **Results** (policy eval, latency Before / P21 / P27 key-pool table, A/B placeholder, NLU AFTER rows with BEFORE alongside), each number linked to its `docs/eval/` file and each block with its command; "Verify in 60 s" (fast pytest, oracle eval, local autoplay). The rest of the old README follows unchanged except the UI bullet (React console), project structure, and the closing "still open" line.
- Media: headless Chrome (`--headless=new`, CDP `Page.startScreencast`, 1440×900, light) over a local server, `counter_ladder` autoplay, operator lens; frames concatenated by their screencast timestamps → H.264 MP4 (25.6 s) → GIF via `palettegen`/`paletteuse`. README media total 2.5 MB.
- Keep-warm: only `workflow_dispatch` is active; the 10-minute `schedule` is commented out with a note that the owner enables it (orchestrator rule: pushing must not ping Render). `/healthz` already existed (Phase 22).
- A/B: README Results and ADR 1 carry `<!-- AB-PENDING -->` … `<!-- /AB-PENDING -->` blocks ("A/B in progress"). No preliminary 24b numbers are used anywhere; ADR 1 cites only the committed policy eval.
- Checks: `/healthz` locally 200 in < 3 ms warm; autoplay locally: counter_ladder deal in 5 turns (both at the default 1.2 s pause and at 3.8 s). Oracle eval `eval_20261007_155510_s7`: thresholds PASS, metrics table identical to `docs/eval/policy_eval_20261006/summary.md`. `uv run pytest -q` 702 passed / 2 skipped; `uv run ruff check .` clean.
- Carry-over:
  - [20.5] Frozen transcripts keep the old opening (pack not regenerated, so summary and transcripts stay one run); dated `NOTE.md` added and linked from `docs/eval/README.md` and README. Test: `test_frozen_transcripts_with_old_opening_carry_a_note`.
  - [20.6] Open issues list refreshed (above); `.gitkeep` removed from `config/` and `eval/`. Test: `test_no_gitkeep_in_non_empty_dirs`.
  - [22.2] README and ADR 3 say the operator view is public by design (synthetic data) and the rep view is the privacy-scoped stream. Test: `test_operator_view_is_documented_as_public_by_design`.
  - [23b.4/26.3] Live check (Chrome, local server :8025, demo profile, live Groq NLU, `?view=rep`, easy_deal, the card's 7 suggested replies clicked in order with `Correct.` for the read-back): **deal**. 7 creditor turns, 8 agent turns: READ_BACK(payment_structure) → ASK_SETTLEMENT → COUNTER 31% → COUNTER 36% → CONFIRM_SCHEDULE(rep_firm, 40%) → PROPOSE_WRAP(confirmed) → CLOSE(thanks_accept). 158 frames, none with `affordability` / `max_bp` / fee / balance / `additional_funds` keys. Card drift noted: the suggestion "Thirty-two is too low" answers a 31% counter.
- Link check: `test_relative_links_and_anchors_resolve` checks every relative link and GitHub heading anchor in README, DESIGN and the eval/assets READMEs; `test_readme_media_within_size_budget` caps README media at 5 MB; `test_keepwarm_workflow_is_manual_only`; `test_ab_pending_markers_are_paired`.
- Deviations: the recording raised the autoplay pause to 3.8 s in the page's start message (the UI has no setting for it) so the run lasts 20–30 s; stated in the README caption and `docs/assets/README.md`. Text inside `docs/history/*` (old phase prompts naming `docs/PLAN.md`) is left as historical record; only live references were fixed. The hosted-demo acceptance item ("responds within 3 s warm and autoplay completes there") is **pending a deploy** (user step); not called per orchestrator rule.
- Open issues: hosted demo check after deploy; enable the keep-warm schedule; A/B refresh of the AB-PENDING blocks; browser-measured voice latency; easy_deal card line "Thirty-two" vs the 31% counter; the easy_deal CONFIRM offered "2 payments totaling $500" though the rep allows 8 (engine shape choice; not investigated); read-back copy "Please confirm the tentative payment structure as even." reads awkwardly.

### Phase 29 (hermetic tests) (2026-10-07) — tests ignore `.env` (fix CI)

- Cause: `app.config.Settings` reads `.env` (`env_file=".env"`) and the process env. The developer `.env` sets `OPENING_DISCLOSURE="You are speaking with an automated agent authorized to discuss settlement options."`, and `tests/data/policy_arm_golden.json` was recorded with it. CI has no `.env`, so it uses the code default ("I am authorized to discuss settlement options for this account.") and `test_policy_agent_output_identical_to_pre_24a_runner` failed on main. Every local gate (pytest, oracle eval) ran with the `.env` present, which hid the failure.
- Files: `tests/conftest.py` (autouse fixture), `tests/unit/test_hermetic_settings.py` (new), `tests/data/policy_arm_golden.json` (re-recorded with code defaults; the diff is only the 5 disclosure lines). No `app/`, `eval/`, `config/` or `.github/` changes.
- Interfaces:
  - `tests/conftest.py::_hermetic_settings` (autouse): sets `Settings.model_config["env_file"] = None` (monkeypatched), unsets every env var `Settings` would read, and clears `get_settings` before and after each test. The var list comes from `settings_env_vars()`: env names whose upper case matches a `Settings.model_fields` name, plus `app.config._POOL_VAR_RE` (`*_KEY` / `*_KEY_<n>`) pool vars. Nothing is hardcoded, so a new Settings field is covered on its own.
  - `tests.conftest.settings_env_vars() -> set[str]`.
  - Exempt: tests marked `@pytest.mark.live` or under `tests/live/`. They keep reading the real `.env` and keys and still opt in with `DSA_LIVE=1`.
  - A test that wants a non-default setting must pass it explicitly (`Settings(x=...)`) or call `monkeypatch.setenv` inside the test. Neither the shell env nor `.env` reaches it.
- Regression: `test_dotenv_in_cwd_is_ignored` writes a `.env` (disclosure, firm, NLG mode, max turns, pool and declared keys) into the cwd and asserts `Settings()` and `get_settings()` give code defaults. `test_dotenv_fixture_is_not_vacuous` shows the same file is read when passed explicitly. `test_no_settings_env_vars_reach_tests` checks that no override var survives into a test.
- Deviations: none.
- Open issues: `eval.run_eval` and the app still read `.env` at runtime (intended). The oracle CI eval output does not include the disclosure line in its metrics, so it was never affected.
- Checks: `uv run pytest -q -m "not slow"` 692 passed / 2 skipped / 3 deselected both **with** the local `.env` and with it moved away (`mv .env .env.bak`, then restored). Full `uv run pytest -q` 695 passed / 2 skipped. `uv run ruff check .` clean. Oracle eval `eval_20261007_155728_s7` thresholds PASS.
