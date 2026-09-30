# Debt Settlement Agent progress log

Each phase appends its handoff here. Keep entries short: facts later phases need, not narration.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Scaffold and vendored engine | done |
| 1 | Domain model | done |
| 2 | Engine adapter and validator | pending |
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

## Interfaces

### `app.config`
- `class Settings(BaseSettings)` — fields: `groq_api_key`, `mistral_api_key`, `gemini_api_key`, `openrouter_api_key`, `cerebras_api_key` (`str | None`); `llm_profile` (`str`, default `"demo"`); `llm_cache` (`bool`); `llm_cache_path` (`str`); `nlg_mode` / `nlu_mode` (`str`); `db_path` (`str`); `hostility_threshold` (`float`); `max_turns` / `max_counters` (`int`); `anchor_ratio` / `concession_factor` (`float`); `firm_name` / `opening_disclosure` (`str`).
- `get_settings() -> Settings`

### `app.domain.money`
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

## Deviations from PLAN.md

- Ruff `extend-exclude = ["feasibility"]` so vendored engine stays untouched (UP035 on `shapes.py` otherwise).
- Synthetic fixtures under `fixtures/engine/{even_ok,rescue_gap,balloon_ok,tier_ok}` replace take-home `cases/`; engine tests re-pointed; rescue expected amounts match `rescue_gap` (lump 17500, incr 4375 × 5 drafts).
- `max_token_pays` has no static `default_factory`; BeliefState keeps it ASSUMED and copies `max_payments` when that field is observed (PLAN: default is max_payments / no limit).

## Open issues

(none yet)

## Phase handoffs

### Phase 0 (2026-09-30)
- Files: `pyproject.toml`, `.gitignore`, `.env.example`, `.venv/` (local), `feasibility/` (vendored), `app/` (+ empty subpackages), `sim/`, `eval/`, `config/`, `tests/{engine,unit,e2e}/`, `fixtures/engine/*`, `app/config.py`, `tests/engine/test_units.py`, `tests/engine/test_rescue.py`, `uv.lock`.
- Tests: 32 passed in `tests/engine`.
- Notes: no ASSIGNMENT.md or cases/ copied.

### Phase 1 (2026-10-01)
- Files: `app/domain/{money,fields,facts,belief,scenario}.py`, `fixtures/demo/{client,offer,firm}.json`, `tests/unit/{test_money,test_fields_facts,test_belief}.py`.
- Tests: 73 passed (engine + unit).
- Notes: demo fixture is synthetic (NorthPeak Collections); independent of `fixtures/engine`.
