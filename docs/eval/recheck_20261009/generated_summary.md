# Eval summary

- run_id: `eval_20261009_062530_s7`  seed=7  profile=`eval`  nlu=`llm`  nlg=`template`  sim=`llm`  oracle_overlay=`False`
- git: `782ca92e51bd8c53d98dbfa90509a930c006c689`
- model share: anthropic/claude-haiku-5-5=49.8%, groq/openai/gpt-oss-120b=50.2%

| metric | value | n | 95% CI |
|---|---|---|---|
| n_completed / n_scenarios | 48/48 | | |
| skipped_quota | 0 | | |
| agreement_valid | 1 | 10 | 0.722–1.000 |
| deal_rate_given_zopa | 0.909 | 11 | 0.623–0.984 |
| no_deal_correct | 0.900 | 10 | 0.596–0.982 |
| escalation_correct | 1 | 37 | 0.906–1.000 |
| rule_extraction_accuracy | 0.619 | 336 | 0.566–0.669 |
| false_known_rate | 0.010 | 210 | 0.003–0.034 |
| stuck_rate | 0.000 | 48 | 0.000–0.074 |
| unverified_figures_spoken | 0 | | |
| sensitive_leaks | 0 | | |
| guard_blocks | 0 | | |
| counters_spoken_max | 6 | | |
| max_counters | 6 | | |
| counters_spoken (mean) | 1.167 | | |
| identical_consecutive_agent_moves | 8 | | |
| stuck_calls | 0 | | |
| turns_to_outcome (mean) | 5.354 | | |
| readback_count (mean) | 0.188 | | |
| turns_to_proposal (mean) | 6.300 | | |
| surplus_captured (mean) | 0.957 | | |

## Price ladder branches (agent moves, informational)

| branch | count |
|---|---|
| anchor | 16 |
| hold | 12 |
| quarter_step | 1 |
| concede_half | 18 |
| reanchor_after_term_change | 0 |
| final_counter | 9 |
| our_counter_accepted | 2 |
| accept_on_repeat | 7 |
| accept_after_holds | 0 |
| counters_exhausted | 0 |
| above_accept_line | 5 |
| max_counters_handoff | 2 |
| loop_guard_repeated_question | 1 |
| loop_guard_no_progress | 0 |
| loop_guard_max_turns | 0 |

## Sim rewrite fallbacks (informational)

- 15 of 257 LLM rewrites dropped for the draft (rate 0.058); by reason: figures=3, stance=12

## Latency (ms)

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 1367.670 | 2561.880 | 305 |
| policy_ms | 0.105 | 1.477 | 305 |
| nlg_ms | 0.123 | 0.336 | 305 |
| server_total_ms | 1455.419 | 2563.097 | 305 |
