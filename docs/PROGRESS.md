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

## Environment facts
- Engine timing (measured before phase 0): a 100-point settlement scan takes 17–261 ms per case.
- Feasibility by settlement % is non-monotonic (for example `0000111100001111...`), and low percentages fail on payment floors.
- Ollama 0.30.10 is installed. Pull `qwen3.5:9b` and `gemma4:e4b` before using the `local` profile.
- Python 3.12 via `uv venv --python 3.12`. Ruff excludes vendored `feasibility/`.
- Demo affordability scan (`range(100, 10001, 100)`, even rules, `fixtures/demo`): median **8.3 ms** cold (5 runs: 8.0–8.5 ms); `lru_cache` hit is sub-ms. `max_bp=10000`, 99/100 grid points feasible.

## Interfaces

### `app.config`
- `class Settings(BaseSettings)` — fields: `groq_api_key`, `mistral_api_key`, `gemini_api_key`, `openrouter_api_key`, `cerebras_api_key` (`str | None`); `llm_profile` (`str`, default `"demo"`); `llm_cache` (`bool`); `llm_cache_path` (`str`); `nlg_mode` / `nlu_mode` (`str`); `db_path` (`str`); `hostility_threshold` (`float`); `max_turns` / `max_counters` (`int`); `anchor_ratio` / `concession_factor` (`float`); `firm_name` / `opening_disclosure` (`str`).
- `get_settings() -> Settings`

### `app.domain.units`
- `render_money(cents: int) -> str`
- `parse_money(text: str) -> int`
- `render_pct(bp: int) -> str`
- `parse_pct(text: str) -> int`
- `render_date(d: date, ref: date) -> str`
- `parse_date(text: str, ref: date) -> date`
- `render_count(n: int) -> str`
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
- `class Fact(BaseModel)` — `id: str`, `kind: Literal["money","pct","count","date"]`, `value: int | date`, `visibility: Literal["PUBLIC","PRIVATE"]`, `source: Literal["engine","creditor","config"]`; `render(self, ref: date) -> str`
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
- Re-exported from `app.agent.policy` for existing callers

### `app.domain.nlu_types`
- `ExtractedTerm`, `TurnAnalysis` — shared with sim/oracle; re-exported from `app.agent.nlu_types`

### `app.agent.nlu_types`
- Re-exports `ExtractedTerm`, `TurnAnalysis` from `app.domain.nlu_types`

### `app.agent.policy`
- `Phase`, `Intent`, `Effect`, `Action` re-exported from `app.domain.actions`
- `Agreement`, `NegotiationState` (also `last_confirm_key`, `confirm_rejects`, `assumed_asked`, `clarify_counts`)
- `ask_pct_to_bp(pct: float) -> int`
- `next_counter(*, ask_bp, max_bp, feasible_bps, c_prev, anchor_ratio, concession_factor) -> int`
- `decide(belief, neg, analysis, afford, *, settings=None, rescue_within_guardrail=False, confirm_facts=None, counter_offer_total_cents=None) -> Action`
  - Identical CONFIRM: `reject` → ASK each ASSUMED once then NO_DEAL; other → soft retry then `confirm_unacked`
  - Unresolved CONTRADICTED after 2 CLARIFY → `ESCALATE(contradiction_unresolved)`
  - Identical COUNTER re-offer counts toward `max_counters` even if NLU misses `reject`
  - CONFIRM wrap only on `stance == "accept"` (not `readback_response`); contradiction/tentative before wrap
  - `wants_to_end` → NO_DEAL when not accepting
  - `_confirm_key` fingerprints all CreditorRules fields; `next_counter` → `None` when no legal bp
- `draft_agreement(*, creditor, bp, offer_total, rows, assumed_fields, audit=None, call_id=None) -> Agreement`
- `opening_action(*, settings=None, firm_name=None, opening_disclosure=None) -> Action`

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
- `stratum_counts(scenarios)`, `balanced_quota(n)`

### `sim.creditor`
- `CreditorReply(text, analysis: TurnAnalysis)`
- `CreditorPolicy(scenario, phrasing="template"|"llm", llm=None)`
  - `async respond(action, agent_text="") -> CreditorReply` — sees intent + PUBLIC facts only; validates `CONFIRM_SCHEDULE` under true rules; concedes 500 bp per rejected counter down to floor; emits oracle `TurnAnalysis`

### `eval.metrics`
- `load_scenario_results(run_dir) -> list[dict]`
- `aggregate(results) -> dict` — PLAN §10 metrics + latency p50/p95; empty denom → null (never vacuous 1.0); WRAP-sans-agreement counts invalid
- `write_summaries(run_dir, summary, *, run_meta=None) -> (summary.json, summary.md)`
- `load_thresholds(path=None) -> dict`
- `check_thresholds(summary, thresholds=None) -> list[str]` (empty ⇒ pass); gates include `no_deal_correct>=0.9`, `deal_rate_given_zopa>=0.75`

### `eval.run_eval`
- CLI: `python -m eval.run_eval --scenarios N --seed S [--resume RUN_ID] [--profile] [--nlg llm|template] [--sim-phrasing llm|template]`
- Writes `eval/results/<run_id>/<scenario_id>.json` per finish; resume skips `status=ok`, retries `skipped_quota`
- `run.json`: models, call_share, seed, git sha, settings; exit 1 on threshold fail

### `app.agent.nlg`
- `SAFE_FALLBACK: str`; `TEMPLATES: dict[Intent, str]`
- `render_action(action, ref_date, *, creditor_numbers=None, private_blocklist=None, audit=None, call_id=None) -> list[str]` — deterministic template path
- `async speak_action(action, ref_date, *, llm=None, settings=None, last_rep_line="", creditor_numbers=None, private_blocklist=None, audit=None, call_id=None) -> list[str]` — LLM template → template_guard (1 retry) → fallback `TEMPLATES` → fill → rendered_guard

### `app.agent.nlu`
- `repair_stance(stance, utterance) -> str` — deterministic accept/reject override
- `class VerifiedTerm` — `field`, `value`, `quote`, `hedged`, `verified`
- `class VerifiedAnalysis` — terms + TurnAnalysis stance fields + `ask_verified`; `to_turn_analysis() -> TurnAnalysis`
- `normalize_for_quote(text) -> str`; `quote_in_utterance(quote, utterance) -> bool`
- `coerce_analysis_payload(data) -> dict` — field-keyed LLM shapes → `terms[]`
- `post_verify(analysis, utterance, *, ref=None, audit=None, call_id=None) -> VerifiedAnalysis`
- `async analyze(utterance, last_agent_line, pending_readback, *, llm=None, settings=None, oracle=None, audit=None, call_id=None, ref=None) -> VerifiedAnalysis`
- `NLU_MODE=oracle` requires `oracle=TurnAnalysis` (skips LLM); re-raises `LLMUnavailable` for eval `skipped_quota`

### `app.agent.session`
- `class Turn` — `role` (`agent`|`creditor`), `text`, `spoken`, `sentence_id`
- `class PendingSpeech` — `action`, `sentence_ids`, `acked`
- `class CallSession` — `call_id`, `scenario`, `belief`, `neg: NegotiationState`, `history`, `pending`, `last_eval`, `agreed_bp`, `agreement`, `last_max_bp`, `creditor_numbers`, `private_blocklist`, `last_belief_changes`, `last_blocked`; props `phase`, `turn_idx`; `last_agent_line()`

### `app.agent.orchestrator`
- `class Utterance` — `sentences: list[tuple[id, text]]`, `action`, `timings`, `belief_changes`, `agreement`
- `apply_effects(session, effects) -> None`
- `class Orchestrator(session, *, llm=None, settings=None, audit=None, auto_ack=False)`
  - `async start() -> Utterance`
  - `async on_creditor_text(text, timings=None, *, oracle=None) -> Utterance` — cancel-and-merge during NLU; queue after NLU; affordability via `asyncio.to_thread`; timings `nlu_ms`/`policy_ms`/`nlg_ms`/`server_total_ms`
  - `async on_sentence_done(ids) -> Agreement | None` — commits effects when all pending sentences acked; drafts agreement on `PROPOSE_WRAP` after validator pass
  - `async on_barge_in(spoken_ids) -> None` — drops pending effects; keeps belief

### `app.cli`
- `python -m app.cli [fixtures/demo]` — type as rep; auto-acks; prints lines, belief, timings, verdict

### `app.voice.stt`
- `async def transcribe(llm, wav_bytes, *, prompt=None) -> tuple[str, float]` — `(text, stt_ms)` via client `transcribe`

### `app.voice.ws`
- `configure(audit, llm, settings=None) -> None`
- WS `/ws/call/{call_id}` — client: `start{scenario}`, binary wav, `text`, `sentence_done{id}`, `barge_in{spoken_ids}`, `timing{turn,vad_end_to_first_audio_ms}`; optional `oracle` on `text` when `nlu_mode=oracle`
- server: `transcript`, `say`, `belief`, `eval` (incl. `max_bp`), `blocked`, `escalate`, `latency`, `audit`, `phase`, `agreement`, `turn_done`

### `app.main`
- `create_app(*, settings=None, llm=None, audit=None) -> FastAPI`
- `GET /`, `GET /static/*`, `GET /metrics/summary` → `{stage: {p50,p95,n}}`
- `app = create_app()` for `uvicorn app.main:app`

### `app.llm.prompts`
- `PLACEHOLDER_MEANINGS: dict[str, str]`
- `nlu_messages(utterance, last_agent_line, pending_readback) -> list[dict]`
- `nlg_messages(intent, placeholder_ids, last_rep_line) -> list[dict]`

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
- Roles: `nlu` | `nlg` | `sim` | `stt`. Routing from `config/providers.yaml` profiles (`demo`/`eval`/`local`/`offline`).
- `on_call` meta: `{role, provider, model, latency_ms, prompt_tokens, completion_tokens, cache_hit, failover_from}`

## Deviations from PLAN.md

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
- NLU `max_tokens=800` and NLG `max_tokens=400` (PLAN said 80 for NLG) because gpt-oss reasoning tokens consume the completion budget.
- `Action`/`Intent`/`Phase`/`Effect` and `TurnAnalysis`/`ExtractedTerm` live in `app.domain` so `sim/` never imports `app.agent` (agent modules re-export).
- Pressuring private-info turns are (2, 3) not PLAN's (3, 5) so short rescue/no_fix calls still escalate offline.
- Counter ladder treats "at max" as the highest feasible counter strictly below the ask (ceiling), not raw `max_bp`, so unreachable asks NO_DEAL instead of looping.
- `ExtractedTerm.value` allows `date` (needed for `first_payment_date` oracle/sim reveals); PLAN listed only int|str|dict.
- Deterministic `repair_stance` after NLU: Gemini often labels "Agreed" / schedule-accept lines as `info`.
- FPD field ask/readback copy avoids the number-word `first` so `template_guard` does not block ASK.
- WS framing adds server `turn_done` after each turn / ack / barge / timing batch so clients can drain without blocking (not named in PLAN §8 event list).

## Open issues

- Mistral chat blocked until Experiment setup (`limit-req-minute=0`); routed last so demo/eval still work via Groq/Gemini.
- Cerebras smoke still probes `llama-3.3-70b` (wrong id; live models are `gpt-oss-120b` / `qwen-3.8-27b`) — not in role routes.
- Local Ollama NLU (`qwen3.5:9b`): ~185 s p95; full 12-scenario local run finishes but quality fails thresholds (`escalation_correct=0`, all scenarios END).

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