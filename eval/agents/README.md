# A/B arms (Phase 24a handoff)

Eval-only harness for the experiment in `docs/REVIEW_PLAN.md` §2(c). It answers one
question: does an LLM that picks moves and writes numbers beat the code policy?

> **Approved rule break, eval-only.** Arms `react` and `llm_only` let an LLM choose
> moves and write spoken numbers, and their prompts carry client financials and the
> private engine ceiling. That breaks two CLAUDE.md ground rules on purpose, because
> that is what the experiment measures. Nothing under `app/` imports `eval.agents`.

## Arms

| `--agent` | Arm | Moves | Words | LLM calls / turn |
|---|---|---|---|---|
| `policy` (default) | A, production | `decide()` | templates / LLM NLG with guards | NLU + ≤1 NLG |
| `policy_h3` | B, H3 (Phase 24b) | `decide()` (identical moves) | + ack / answer acts, 3-turn NLG context | NLU + ≤1 NLG (0 with bank) |
| `react` | C, ReAct | LLM, up to 4 tool steps, engine-guarded | LLM, unguarded | NLU + 1–4 |
| `llm_only` | D, baseline | LLM, one JSON move, unguarded | LLM, unguarded | NLU + 1 |

Arm B is the production orchestrator with `Settings.nlg_h3` on (`make_agent`
copies the settings with `nlg_h3=True`). The A/B ran it with `--nlg bank`
(the demo NLG mode). Results: `docs/eval/ab_20261007/summary.md`.

Shared by `react` and `llm_only` (`base.py`), so the arms differ only in how the
move is chosen:
- **Perception:** the same NLU (`app.agent.nlu.analyze`, or the sim oracle under
  `--nlu oracle`) feeds a `BeliefState` the same way the orchestrator does.
  Rule-extraction metrics therefore compare like with like.
- **Engine:** affordability every turn. It is audited as `engine/affordability`, so
  the runner's leak scan includes every ceiling the agent saw.
- **Opening:** the same disclosed opening line as the policy, with no LLM.
- **Move → `Action`:** each terminal tool maps one-to-one onto an `Intent`, with
  engine-backed PUBLIC facts. `sim.creditor.CreditorPolicy.respond` is unchanged.
- **Wrap:** the agreement is drafted from the last confirmed schedule and checked
  by the validator. A wrap with no valid schedule stays in `WRAP` with no
  agreement, so it scores `agreement_valid = 0`. (The orchestrator falls back to
  `END` instead. The LLM arms do not get that softer outcome, because the LLM has
  already told the rep the deal is done.)

Tools / moves: `get_rules`, `evaluate_offer(bp)` (observations; ReAct only),
`ask(field)`, `ask_settlement`, `read_back(field)`, `clarify(field)`,
`propose_counter(bp)`, `propose_terms(field, value)`, `confirm_schedule(bp)`,
`propose_wrap`, `refuse_private`, `refuse_commit`, `escalate(reason)`,
`end_no_deal(reason)`, `say(text)`. The prompts are documented in the module
docstrings of `react_agent.py` and `llm_only_agent.py`.

## Run

```bash
# CI path, unchanged (policy, offline, no keys):
uv run python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7

# One arm on the eval profile (keys needed for react / llm_only):
uv run python -m eval.run_eval --agent react --scenarios 48 --seed 7 --profile eval \
  --nlg template --sim-phrasing llm --no-oracle-overlay

# Naturalness, blind pairwise (not a gate). Writes eval/results/judge_<stamp>/:
uv run python -m eval.judge_naturalness <RUN_A> <RUN_B> --profile eval

# Two arms at once: give each process half the per-key rates.
uv run python -m eval.run_eval ... --providers docs/eval/ab_20261007/providers_split2.yaml

# One table over the arms (common scenarios only) + adoption rule:
uv run python -m eval.ab_report --arm A=<RUN_A> --arm B=<RUN_B> ... \
  --judge B:A=<JUDGE_DIR> --out docs/eval/ab_<date> [--decision FILE] [--notes FILE]
```

With `--nlu oracle`, the LLM arms keep `--profile`, because their moves need a real
LLM. Only the policy arm is forced onto `offline`.

## Outputs

- Per-call JSON adds `agent`, `transcript` (agent and rep lines), `llm_calls_per_turn`
  and `turn_latency_ms`. `llm_calls_per_turn` counts every non-`sim` LLM attempt in
  that agent turn, failovers included, via the runner's `on_call` hook.
- `run.json["arm_metrics"]` (`arm_metrics.py`) holds the mean, p95 and max of LLM
  calls per turn, plus turn latency p50 and p95.
- `summary.md` / `summary.json` and the threshold gate are unchanged for every arm.
  The leak scan, the unverified re-scan, the validator, and the outcome metrics all
  apply to every arm.
- Judge: `judge.json` (verdicts per scenario), `summary.json` (A win-rate over
  decisive pairs with a Wilson 95% CI, plus ties), `human_pairs.csv` (20 random
  pairs with sides shuffled and a blank rating column), and `human_pairs_key.csv`.

## Notes

- **Routing (Phase 24b):** agent steps use client role `agent`
  (`base.AGENT_ROLE`), bounded by `Settings.llm_timeout_agent_s` (20 s). Only
  the `eval` profile has an `agent` route (Groq → Cerebras → Gemini, free tier);
  other profiles fall back to their `nlu` route.
- **No native tool calling:** the client exposes `chat_text` / `chat_json` only, so
  ReAct uses a JSON tool-call protocol (`{"thought", "tool", "args", "text"}`),
  not provider function-calling.
- **The sim reacts to intents, not text.** `say(text)` is sent as
  `ASK_SETTLEMENT` (`reason="say"`), and the rep restates its position. A spoken
  number that does not match the move's facts does not change the sim's reply. It
  only shows up in `unverified_figures_spoken` / `sensitive_leaks`.
- **`coerce_bp`:** the prompts ask for every bp argument as a `"N%"` string. An
  explicit `"N%"` is exact (`"1%"` → 100 bp; before 24b it was wrongly re-scaled
  to 100%). A bare number ≤ 100 is still read as percent (`45` → 4500 bp).
- **Pre-registered adoption rule** (REVIEW_PLAN §2(c)): zero leaks and zero
  unverified figures; `agreement_valid` = 1; outcome rates within A's 95% CI;
  p50 latency ≤ A + 0.5 s; naturalness win-rate ≥ 60%.
