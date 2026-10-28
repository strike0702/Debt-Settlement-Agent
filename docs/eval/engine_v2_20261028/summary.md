# Engine v2, Phase 61: oracle eval on the finished engine (2026-10-28)

Offline oracle eval, the CI gate: oracle NLU, template NLG, template creditor
phrasing, 100 scenarios, seed 7, `offline` profile (no network, no keys), `.env` moved
aside (code defaults: `ENGINE_OBJECTIVE=fee_first`, `ENGINE_FEE_POLICY=front_loaded`, no
firm cushion).

```bash
python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7
```

- **Phase 61** (`legacy.py` and the gap-report code removed, docs): `eval_20261028_171153_s7`
  on branch `phase-61`. [`generated_summary.md`](generated_summary.md),
  [`summary.json`](summary.json) and [`run.json`](run.json) are that run's files.
- **Main** before Phase 61 (`25fd375`, Phases 55–60 merged), from a `git archive`
  export: `eval_20261028_171245_s7`, [`main_summary.json`](main_summary.json).
- **Thresholds: PASS** on both. `summary.json` is identical apart from `latency`, and all
  100 per-call records are identical apart from timings. Phase 61 changes no engine
  output.

| metric | value |
|---|---|
| agreement_valid | 1 (21 deals) |
| deal_rate_given_zopa | 0.913 |
| no_deal_correct / escalation_correct | 1 / 1 |
| unverified_figures_spoken / sensitive_leaks / guard_blocks | 0 / 0 / 0 |
| turns_to_outcome (mean) | 5.38 |
| surplus_captured (mean) | 0.873 |
| client cushion at the agreed price, median (report only) | $237.32 drafted (fee-first); client-first would be $799.87 |

These are the same values as the Phase 50 policy eval and every engine phase since: the
exact search found no miss on the scenarios (the take-home family was exact there), so
no call changed. The engine benchmark of the same build is in [`bench.md`](bench.md).
