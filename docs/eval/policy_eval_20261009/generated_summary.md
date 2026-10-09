# Eval summary

- run_id: `eval_20261009_014049_s7`  seed=7  profile=`offline`  nlu=`oracle`  nlg=`template`  sim=`template`  oracle_overlay=`True`
- git: `15e0540dee2a911c297813e0562c4e4abae808ad`

| metric | value | n | 95% CI |
|---|---|---|---|
| n_completed / n_scenarios | 100/100 | | |
| skipped_quota | 0 | | |
| agreement_valid | 1 | 23 | 0.857–1.000 |
| deal_rate_given_zopa | 1 | 23 | 0.857–1.000 |
| no_deal_correct | 1 | 22 | 0.851–1.000 |
| escalation_correct | 1 | 77 | 0.952–1.000 |
| rule_extraction_accuracy | 0.670 | 700 | 0.634–0.704 |
| false_known_rate | 0.000 | 469 | 0.000–0.008 |
| stuck_rate | 0.000 | 100 | 0.000–0.037 |
| unverified_figures_spoken | 0 | | |
| sensitive_leaks | 0 | | |
| guard_blocks | 0 | | |
| counters_spoken_max | 2 | | |
| max_counters | 4 | | |
| counters_spoken (mean) | 0.440 | | |
| identical_consecutive_agent_moves | 0 | | |
| stuck_calls | 0 | | |
| turns_to_outcome (mean) | 4.610 | | |
| readback_count (mean) | 0.230 | | |
| turns_to_proposal (mean) | 3.957 | | |
| surplus_captured (mean) | 0.689 | | |

## Latency (ms)

| stage | p50 | p95 | n |
|---|---|---|---|
| nlu_ms | 0.043 | 0.227 | 561 |
| policy_ms | 0.023 | 0.772 | 561 |
| nlg_ms | 0.052 | 0.091 | 561 |
| server_total_ms | 0.727 | 311.786 | 561 |
