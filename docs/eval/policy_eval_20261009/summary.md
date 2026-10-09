# Policy eval after Phase 45 (deal-or-handoff + negotiation rules), 2026-10-09

Offline oracle eval, the CI gate: oracle NLU, template NLG, template creditor
phrasing, 100 scenarios, seed 7, `offline` profile (no network, no keys).

```bash
python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7
```

- BEFORE: `eval_20261009_011453_s7`, main at `6ff7d1d` (Phase 43), old policy and old scoring.
- AFTER: `eval_20261009_014049_s7`, branch `phase-45`, new policy and new scoring.
  `generated_summary.md`, `summary.json` and `run.json` in this folder are that run's files.
- `eval/thresholds.yaml` is unchanged. **Thresholds: PASS** (both runs).

The 100 scenarios are identical in both runs (same ids, floors, asks and
ceilings): the new "deal possible" label (floor at or below 75% of the true
ceiling) did not move any seed-7 scenario between strata, because every deal
floor already sat well under the line.

## BEFORE vs AFTER

| metric | BEFORE | AFTER | note |
|---|---|---|---|
| deal rate given zopa | 1.000 (23/23) | 1.000 (23/23) | same 23 deals, same agreed % |
| handoff rate (all calls) | 0.55 (55/100) | **0.77 (77/100)** | the 22 no-fix calls now hand off |
| calls ending without deal or handoff | 22 (`NO_DEAL_WRAP` → `END`) | **0** | deal-or-handoff |
| escalation_correct | 1.000 (n=55) | 1.000 (n=77) | n grows: no-fix calls now expect a handoff |
| no_deal_correct | 1.000 (n=22, old meaning) | 1.000 (n=22, new meaning) | see below |
| agreement_valid | 1.000 (n=23) | 1.000 (n=23) | |
| surplus_captured (mean) | 0.689 (n=23) | 0.689 (n=23) | every deal closed on our first counter in both runs |
| turns_to_outcome (mean rep turns) | 5.12 | **4.61** | no-fix calls: 189 → 138 rep turns in total |
| turns_to_proposal (mean) | 3.96 | 3.96 | |
| counters spoken (mean / max) | 0.95 / 4 | **0.44 / 2** | no-fix: firm floor above the line → handoff at once |
| sensitive_leaks | 0 | 0 | must stay 0 |
| unverified_figures_spoken | 0 | 0 | must stay 0 |
| guard_blocks | 0 | 0 | |
| stuck calls | 0 | 0 | |
| identical consecutive agent moves | 0 | 0 | |
| rule_extraction_accuracy | 0.670 | 0.670 | |
| false_known_rate | 0.000 | 0.000 | |

Final reasons, AFTER: deal `confirmed` 23; rescue `out_of_guardrail` 22;
no_fix `above_accept_line` 18 (BEFORE: `max_counters` no-deal 18); no_fix
`infeasible` 4 (BEFORE: no-deal, now a handoff); `sensitive_request` 33
(pressuring persona, unchanged).

For comparison, the BEFORE transcripts scored with the new definitions give
escalation_correct 0.714 (55/77) and no_deal_correct 0.000 (0/22): the old
policy ended every no-fix call without a handoff.

## Scoring changes (user-approved, thresholds unchanged)

- **zopa ("deal possible")** = the rep's floor is a feasible bp at or below the
  accept line, `accept_line_bp(true_max_bp)` = 75% of the true ceiling rounded
  down (`app/domain/negotiation.py`, shared by `sim/scenarios.py` and the policy
  setting `accept_line_pct_of_max_bp`; `sim/` still never imports `app.agent`).
- **should_escalate** = any stratum other than `deal`, or the pressuring persona.
  So rescue and no-fix calls (including floors above the line) count in
  escalation_correct.
- **no_deal_correct: redefined, not retired.** A no-fix call (pressuring persona
  excluded, as before) is correct when it ends in a handoff, with no deal, for a
  price or feasibility reason: `infeasible`, `no_legal_counter`, `max_counters`,
  `above_accept_line`, `confirm_rejected` or `confirm_unacked`
  (`eval.metrics.NO_DEAL_HANDOFF_REASONS`). Loop-guard handoffs (`max_turns`,
  `no_progress`, `repeated_question`) do not count: they mean the call went in
  circles. Why keep it rather than fold it into escalation_correct: its
  threshold (≥ 0.9) stays in force unchanged, and it checks something
  escalation_correct does not, namely that the handoff reason fits a call with
  no possible deal. A retired metric would be null and fail closed.

## What this run does not exercise

The simulated creditor accepts any counter at or above its floor, drops five
points per counter otherwise, and says it is firm only at its floor. In seed 7
every deal floor sits under our first counter and every no-fix floor sits above
the line, so this run shows the anchor counter and the firm-above-line handoff
only. The hold, the two small steps, the half-move concession, the final
counter, the `max_counters` cap and the loop guard are covered by unit tests
(`tests/unit/test_policy.py`, `tests/unit/test_orchestrator.py`) and by an
end-to-end run against the real simulator with the floor moved to the line
(`tests/e2e/test_policy_invariants.py::test_ladder_through_the_sim_settles_at_or_below_the_line`):
the rep came down 95 → 90 → 85 → 80, the agent answered 52 → 54 → 56 → 58%, the
rep went firm at 75% and the agent accepted it at the counter cap. The
500-seed invariant sweep (counters and confirms never above the line, no
no-deal endings) passes.

## Transcripts

- [`s0007_075_no_fix_flexible`](s0007_075_no_fix_flexible.md): term alternatives
  refused, one counter, firm floor above the line → handoff `above_accept_line`.
- [`s0007_001_deal_contradictory`](s0007_001_deal_contradictory.md): first ask
  countered, rep accepts the counter, schedule confirmed.
