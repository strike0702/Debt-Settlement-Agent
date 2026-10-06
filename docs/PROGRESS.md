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
| 14 | `decide()` bugs, bounded extract, invariants (ROADMAP) | done |
| 15 | NLU and safety corpus, flag fixes | done |

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
- Re-exported from `app.agent.policy` for existing callers

### `app.domain.nlu_types`
- `ExtractedTerm`, `TurnAnalysis` — shared with sim/oracle; re-exported from `app.agent.nlu_types`

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
- CLI: `python -m eval.run_eval --scenarios N --seed S [--resume RUN_ID] [--profile] [--nlu oracle|llm] [--nlg llm|template] [--sim-phrasing llm|template] [--no-oracle-overlay]`
  - `--nlu oracle`: forces profile `offline` (FakeLLM), no network/keys; requires `--nlg template --sim-phrasing template`; rejects `--no-oracle-overlay`
  - `--nlu llm` (default): live NLU, sim disposition overlay on unless `--no-oracle-overlay`
- `_build_settings(*, profile, nlg, nlu="llm", base=None) -> Settings`
- `run_one_scenario(scenario, *, settings, llm, sim_phrasing, audit_dir, max_turns=None, oracle_overlay=True) -> dict` — per-call keys add `counters_spoken`, `max_counters`, `identical_consecutive_agent_moves`, `turns_to_outcome`, `hit_max_turns`, `final_reason`
- Leak scan = client/firm private amounts ∪ engine-private (`true_max_bp`, every logged affordability `max_bp`, true-rules rescue lump/increment); engine-private values that were spoken as a PUBLIC fact are exempt
- Writes `eval/results/<run_id>/<scenario_id>.json` per finish; resume skips `status=ok`, retries `skipped_quota`
- `run.json`: models, call_share, seed, git sha, settings, `nlu`, `oracle_overlay`; exit 1 on threshold fail

### `app.agent.nlg`
- `SAFE_FALLBACK: str`; `TEMPLATES: dict[Intent, str]`
- `render_action(action, ref_date, *, creditor_numbers=None, private_blocklist=None, audit=None, call_id=None) -> list[str]` — deterministic template path
- `async speak_action(action, ref_date, *, llm=None, settings=None, last_rep_line="", creditor_numbers=None, private_blocklist=None, audit=None, call_id=None) -> list[str]` — LLM template → template_guard (1 retry) → fallback `TEMPLATES` → fill → rendered_guard

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
- `nlu_messages(utterance, last_agent_line, pending_readback, *, ref=None) -> list[dict]` — `ref` adds a `Today's date:` line (`analyze` passes its `ref`)
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
- Invariants: `tests/e2e/test_policy_invariants.py::test_policy_invariants_over_seeds` (`@pytest.mark.slow`) runs one scenario per seed (stratum × persona cycling, sub-seed from `Random(seed)`). It checks: COUNTER < ask, COUNTER ≤ `last_max_bp`, ≤ `max_counters` COUNTERs, no identical consecutive COUNTER, terminates within `max_turns`, and every WRAP agreement validates. `DSA_INVARIANT_SEEDS` sets the seed count (default 100, so CI and plain `pytest -q` run 100). **500 seeds: pass** (~7 min).
- Eval `eval_20261005_233242_s7` (`--nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`): **thresholds PASS**. `counters_spoken_max` 4 (was 10), `identical_consecutive_agent_moves` 0 (was 54), `turns_to_outcome` mean 5.25 (was 6.11). agreement_valid / deal_rate_given_zopa / no_deal_correct / escalation_correct = 1 with the same n; rule_extraction_accuracy 0.670 (n=700); leaks / unverified / guard_blocks = 0.
- Deviations: "validates under true rules" uses `CreditorPolicy.agreed_rules` (true rules + accepted COUNTER_TERMS), the same choice the Phase 12 eval made. The invariant sweep uses per-seed `generate_one`, not `generate(500, seed)`.
- Cleanup: removed `NegotiationState.rejects`, the `inc_reject_at_max` effect and its orchestrator handler (dead after the fix; dropped the `rejects=` kwarg from 3 policy tests, whose assertions are unchanged). Removed the unreachable `no_counter_below_ask` branch (test: `test_next_counter_always_below_ask_and_within_max`). Merged the duplicate `afford is None` ASK_SETTLEMENT branch into the ask-unknown branch (test: `test_ask_known_but_rules_unbuildable_asks_settlement`). Merged the duplicated term-alt gate for the empty-curve and above-ceiling cases into `_term_alt_action` (test: `test_ask_above_ceiling_fpd_already_countered_ladders`). Dropped the unreachable enum/int fallbacks in `_fact_for_value`, since enums always go through text slots (test: `test_clarify_enum_field_uses_text_slots`). Removed the unused `max_counters` param of `_stall_after_confirm`. Collapsed 8 copies of the confirm `required` set, 2 PROPOSE_WRAP builders, 5 ESCALATE builders and 2 ASK builders into one helper each.
- Observed, not fixed: tiers READ_BACK still speaks `str([])`, so a template reads "[] for the payment tiers" (NLG/registry copy, outside the policy). `next_counter` can return an off-grid `c_prev` when the curve changes; `decide` never re-emits it (stall → jump / no-deal), but the helper contract is loose.
- Tests: 415 passed offline (+1 skipped live), invariants at 100 seeds included; `ruff check .` clean.

### Phase 15 (2026-10-06) — NLU and safety corpus, flag fixes

- Files: `tests/nlu_corpus.jsonl` (177 synthetic hand-labelled lines), `eval/nlu_corpus.py`, `docs/eval/{nlu_corpus.md,nlu_corpus_before.jsonl,nlu_corpus_after.jsonl}`, `tests/unit/{test_nlu_repairs,test_nlu_corpus}.py`; `app/agent/nlu.py`; two tests in `tests/unit/test_nlu_nlg_llm.py` rewritten for the OR rule.
- Interfaces:
  - CLI `python -m eval.nlu_corpus --label BEFORE|AFTER [--profile demo] [--concurrency 4]` — `llm_cache=True`, cache at `eval/nlu_corpus_cache.db` (keyed by provider/model); rewrites only its own `## <LABEL>` section of the report.
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
