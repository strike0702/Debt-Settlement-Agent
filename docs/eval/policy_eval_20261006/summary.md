# Eval summary

- run_id: `eval_20261006_004050_s7`  seed=7  profile=`offline`  nlu=`oracle`  nlg=`template`  sim=`template`  oracle_overlay=`True`
- git: `27520f389454001687585f2db2cf84cd9cf2e009`

| metric | value | n | 95% CI |
|---|---|---|---|
| n_completed / n_scenarios | 100/100 | | |
| skipped_quota | 0 | | |
| agreement_valid | 1 | 23 | 0.857–1.000 |
| deal_rate_given_zopa | 1 | 23 | 0.857–1.000 |
| no_deal_correct | 1 | 22 | 0.851–1.000 |
| escalation_correct | 1 | 55 | 0.935–1.000 |
| rule_extraction_accuracy | 0.670 | 700 | 0.634–0.704 |
| false_known_rate | 0.000 | 469 | 0.000–0.008 |
| stuck_rate | 0.000 | 100 | 0.000–0.037 |
| unverified_figures_spoken | 0 | | |
| sensitive_leaks | 0 | | |
| guard_blocks | 0 | | |
| counters_spoken_max | 4 | | |
| max_counters | 4 | | |
| counters_spoken (mean) | 0.950 | | |
| identical_consecutive_agent_moves | 0 | | |
| stuck_calls | 0 | | |
| turns_to_outcome (mean) | 5.120 | | |
| readback_count (mean) | 0.230 | | |
| turns_to_proposal (mean) | 3.957 | | |
| surplus_captured (mean) | 0.689 | | |

## Latency (ms)

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 0.041 | 0.222 | 612 |
| policy_ms | 0.025 | 0.768 | 612 |
| nlg_ms | 0.055 | 0.180 | 612 |
| server_total_ms | 0.787 | 312.699 | 612 |
