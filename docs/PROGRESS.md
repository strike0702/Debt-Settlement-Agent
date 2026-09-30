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
| 6 | NLU and LLM NLG | pending |
| 7 | Session, orchestrator, CLI | pending |
| 8 | Simulator, scenarios, offline e2e | pending |
| 9 | Eval runner and metrics | pending |
| 10 | Voice and UI | pending |
| 11 | README and final eval | pending |

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

### `app.agent.nlu_types`
- `class ExtractedTerm` — `field` (registry names incl. `min_payment_tiers`), `value`, `quote`, `hedged`
- `class TurnAnalysis` — `terms`, `settlement_ask_pct` (percent points, 45.0 → 4500 bp), `ask_quote`, `stance`, `readback_response`, `asks_client_private_info`, `demands_commitment`, `hostility`, `wants_to_end`

### `app.agent.policy`
- `Phase`, `Intent` (StrEnums); `Effect`, `Action`, `Agreement`, `NegotiationState`
- `ask_pct_to_bp(pct: float) -> int`
- `next_counter(*, ask_bp, max_bp, feasible_bps, c_prev, anchor_ratio, concession_factor) -> int`
- `decide(belief, neg, analysis, afford, *, settings=None, rescue_within_guardrail=False, confirm_facts=None, counter_offer_total_cents=None) -> Action`
- `draft_agreement(*, creditor, bp, offer_total, rows, assumed_fields, audit=None, call_id=None) -> Agreement`
- `opening_action(*, settings=None, firm_name=None, opening_disclosure=None) -> Action`

### `app.agent.nlg`
- `SAFE_FALLBACK: str`; `TEMPLATES: dict[Intent, str]`
- `render_action(action, ref_date, *, creditor_numbers=None, private_blocklist=None) -> list[str]`

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
- OpenRouter smoke: `openai/gpt-oss-120b:free` returns 404 (“unavailable for free”); slug kept as in PLAN until a free replacement is chosen.

## Open issues

- Ollama: daemon up but planned models not pulled (`qwen3.5:9b`, `gemma4:e4b`); only `qwen2.5-coder:7b` present — `local` profile will skip Ollama until pull.
- Cerebras smoke model `llama-3.3-70b` 404 (not in demo/eval routes; only used in smoke probe).

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
