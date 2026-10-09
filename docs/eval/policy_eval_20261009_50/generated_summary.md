# Eval summary

- run_id: `eval_20261009_165345_s7`  seed=7  profile=`offline`  nlu=`oracle`  nlg=`template`  sim=`template`  oracle_overlay=`True`
- git: `062d847203e7a2645313e8d6d570fcc913569279`

| metric | value | n | 95% CI |
|---|---|---|---|
| n_completed / n_scenarios | 100/100 | | |
| skipped_quota | 0 | | |
| agreement_valid | 1 | 21 | 0.845–1.000 |
| deal_rate_given_zopa | 0.913 | 23 | 0.732–0.976 |
| no_deal_correct | 1 | 22 | 0.851–1.000 |
| escalation_correct | 1 | 77 | 0.952–1.000 |
| rule_extraction_accuracy | 0.661 | 700 | 0.626–0.696 |
| false_known_rate | 0.000 | 463 | 0.000–0.008 |
| stuck_rate | 0.000 | 100 | 0.000–0.037 |
| unverified_figures_spoken | 0 | | |
| sensitive_leaks | 0 | | |
| guard_blocks | 0 | | |
| counters_spoken_max | 6 | | |
| max_counters | 6 | | |
| counters_spoken (mean) | 1.260 | | |
| identical_consecutive_agent_moves | 0 | | |
| stuck_calls | 0 | | |
| turns_to_outcome (mean) | 5.380 | | |
| readback_count (mean) | 0.210 | | |
| turns_to_proposal (mean) | 6.238 | | |
| surplus_captured (mean) | 0.873 | | |

## Price ladder branches (agent moves, informational)

| branch | count |
|---|---|
| anchor | 40 |
| hold | 28 |
| equal_step | 9 |
| concede_half | 39 |
| reanchor_after_term_change | 0 |
| final_counter | 10 |
| our_counter_accepted | 9 |
| accept_on_repeat | 10 |
| accept_after_holds | 0 |
| counters_exhausted | 2 |
| above_accept_line | 16 |
| max_counters_handoff | 2 |
| loop_guard_repeated_question | 1 |
| loop_guard_no_progress | 1 |
| loop_guard_max_turns | 0 |

## Sim rewrite fallbacks (informational)

- 0 of 0 LLM rewrites dropped for the draft (rate n/a); by reason: none

## Latency (ms)

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 0.045 | 0.233 | 638 |
| policy_ms | 0.032 | 0.985 | 638 |
| nlg_ms | 0.058 | 0.091 | 638 |
| server_total_ms | 0.792 | 295.143 | 638 |
