# Debt Settlement Agent progress log

Each phase appends its handoff here. Keep entries short: facts later phases need, not narration.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Scaffold and vendored engine | done |
| 1 | Domain model | done |
| 2 | Engine adapter and validator | done |
| 3 | Numbers and guards | pending |
| 4 | Policy, NLG templates, audit | pending |
| 5 | LLM client and provider pool | pending |
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

## Deviations from PLAN.md

- Ruff `extend-exclude = ["feasibility"]` so vendored engine stays untouched (UP035 on `shapes.py` otherwise).
- Synthetic fixtures under `fixtures/engine/{even_ok,rescue_gap,balloon_ok,tier_ok,gap_curve}` replace take-home `cases/`; engine tests re-pointed; rescue expected amounts match `rescue_gap` (lump 17500, incr 4375 × 5 drafts).
- `max_token_pays` has no static `default_factory`; BeliefState keeps it ASSUMED and copies `max_payments` when that field is observed (PLAN: default is max_payments / no limit).
- Domain unit helpers live in `app/domain/units.py` (PLAN/PHASES said `money.py`); covers money, pct, date, and count.
- `evaluate(..., *, assumed=)` optional kwarg so assumed field names can be recorded without re-passing belief.

## Open issues

(none yet)

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
