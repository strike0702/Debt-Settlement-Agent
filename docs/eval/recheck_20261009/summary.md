# Live re-check of agreement validity (Phase 46c), 2026-10-09

The question: does `agreement_valid` hold under live NLU after Phases 45, 46a and 46b?
The Phase 24b A/B measured 0.75 for arm A (policy + template NLG) with live NLU.
This run is that arm on the same 48 seed-7 scenario ids, with two changes made by user
decision: Claude Haiku 5.5 alone reads the rep, and Groq `gpt-oss-120b` plays the rep.

## Setup

- **Agent:** policy, template NLG, code-built acks on. These are current `main` defaults:
  `nlg_ack` True, `max_counters` 6, `nlg_h3` False.
- **NLU:** Claude Haiku 5.5 only (`output_config.effort: low`, `budgeted`, `timeout_s` 20).
  There is no fallback, so the run cannot mix NLU models. 255 of 255 NLU calls were
  answered by Haiku: 0 errors, 0 failovers, 0 budget skips.
- **Sim:** the new eval `sim` route (`config/providers.yaml`): Groq `openai/gpt-oss-120b`
  (low reasoning effort), then Cerebras `gpt-oss-120b` (low), then Gemini 3.1 Flash-Lite.
  `SIM_MAX_TOKENS` is 512 (was 120). All 257 rewrites were answered by Groq.
- **Providers file:** `providers_recheck.yaml` (this folder). It is eval-only and is passed
  with `--providers`, so its budgeted target is kept.
- **Run:** `eval_20261009_062530_s7` (git `782ca92` plus this phase's uncommitted route
  change), 48/48 `status=ok`. Its summary is copied here as `generated_summary.md` and
  `summary.json`.

```bash
uv run python -m eval.run_eval --scenarios 48 --seed 7 --profile eval --nlu llm \
  --nlg template --sim-phrasing llm --no-oracle-overlay --agent policy \
  --providers docs/eval/recheck_20261009/providers_recheck.yaml
```

**Tooling note:** `eval.run_eval._build_settings` does not copy `anthropic_api_key`, so
under `run_eval` an unsuffixed `ANTHROPIC_API_KEY` is lost and Haiku is skipped silently.
The run copied the key into `ANTHROPIC_API_KEY_1`, which the key pool reads. This was a
scratch wrapper that changed no repo code. The fix is deferred because `eval/` is outside
this phase's paths.

## Not the same scenarios as the A/B

The ids, clients, hidden rules and personas match the A/B. Phase 46a then gave **14 of
the 48** a haggle style:

- 9 deal calls became holders or steppers. Their floor moved from about 21–25% to 56–63%.
- 4 no-fix calls became holders or steppers. Their opening ask moved up by 5–20 points.
- 1 deal call became a staller (`s0007_010`, numbers unchanged).

Both of arm A's invalid calls, `s0007_004` (holder) and `s0007_015` (stepper), are among
the 14. Phase 45 also changed how calls are labelled and scored:

- `zopa` now requires floor ≤ accept line.
- `should_escalate` is now stratum ≠ deal, or pressuring.
- `no_deal_correct` now means "handoff with a no-deal reason".

So the strata n differ from the A/B for escalation (37 vs 27). The deal and no-deal n are
11 and 10 in both runs, but they cover different calls under different definitions.

## Results vs A/B arm A (live, 2026-10-07)

| metric | A/B arm A | **re-check** | note |
|---|---|---|---|
| NLU | free chain (Gemini first) | Haiku 5.5 only | |
| rep phrasing | Gemini Flash-Lite, no figure check | Groq gpt-oss-120b + P46a check | |
| **agreement_valid** | 0.75 (6/8) | **1.00 (10/10)** [0.72, 1.00] | 0 invalid calls |
| deal_rate_given_zopa | 0.73 (n=11) | 0.91 (10/11) [0.62, 0.98] | miss: `s0007_010` staller → `repeated_question` (by design) |
| no_deal_correct | 0.90 (n=10, old meaning) | 0.90 (9/10) [0.60, 0.98] | miss: `s0007_040` → `contradiction_unresolved` (below) |
| escalation_correct | 1.00 (n=27, old meaning) | 1.00 (37/37) | |
| sensitive_leaks / unverified figures | 0 / 0 | 0 / 0 | guard_blocks 0 |
| rule_extraction_accuracy | 0.574 (n=336) | 0.619 (n=336) | |
| false_known_rate | 0.059 (n=205) | 0.010 (n=210) | both from the case below |
| sim rewrite fallback rate | not measured | 0.058 (15/257): stance 12, figures 3, empty 0 | |
| turns_to_outcome (mean) | 6.06 | 5.35 | |
| agent LLM calls / turn | 0.88 | 0.84 | all NLU (template NLG) |
| server_total p50 / p95 ms | 1957 / 11932 | **1455 / 2563** | nlu_ms p50 1368 / p95 2562 |
| deals | 8 | 10 | |

**Thresholds** (`eval/thresholds.yaml`, unchanged): **PASS**.

### Handoff and end reasons

| reason | A/B arm A | re-check |
|---|---|---|
| deal `confirmed` | 8 | 10 |
| `sensitive_request` | 16 | 16 |
| `out_of_guardrail` | 11 | 10 |
| `above_accept_line` | – (pre-P45) | 5 |
| `max_counters` | 6 (NO_DEAL_WRAP) | 2 (handoff) |
| `infeasible` | 3 (NO_DEAL_WRAP) | 2 (handoff) |
| `confirm_rejected` | 2 (NO_DEAL_WRAP) | 0 |
| `contradiction_unresolved` | 1 | 2 |
| `repeated_question` | – (pre-P45) | 1 |
| stuck mid-call (`ASK`) | 1 | 0 |

### Price ladder branches (re-check; the A/B predates the ladder)

| anchor | hold | quarter step | concede half | final counter | our counter accepted | accept on repeat | above line | max_counters | repeated_question |
|---|---|---|---|---|---|---|---|---|---|
| 16 | 12 | 1 | 18 | 9 | 2 | 7 | 5 | 2 | 1 |

Branches with 0: re-anchor after a term change, accept after holds (`rep_held`),
`counters_exhausted`, `no_progress`, `max_turns`. `counters_spoken` mean / max:
1.17 / 6.

## Calls whose agreement is not valid

**None.** All 10 drafted agreements pass the validator against the creditor's true rules.

The closest calls are below. None of them drafted an agreement.

| call | what happened | class |
|---|---|---|
| `s0007_040_no_fix_contradictory` | The rewrite added "Actually, we can't." to the opening, and later to the clarify answer: "We can go up to 3 payments. Actually, we can't." Haiku read no terms from that answer, both CLARIFYs went unanswered, and the agent handed off with `contradiction_unresolved` instead of a no-deal reason. This is the one `no_deal_correct` miss. | sim nonsense that slipped the check |
| `s0007_019_rescue_contradictory` | Same pattern: "Sure, the maximum is 6 payments. Actually, it isn't." The handoff was still correct, since this is a rescue call. | sim nonsense that slipped the check |
| `s0007_028_rescue_contradictory`, `s0007_034_no_fix_contradictory` | The persona's change line "Actually, make that a maximum of 6 payments…" was rewritten as "No, actually it's a maximum of 6 payments…". The P46b correction path (`acts.is_ack_correction`: "no" + value, after we acked 4) took that as our misreading. It set the wrong count KNOWN (`belief/ack_corrected`) with no CLARIFY. These are the run's 2 false-KNOWN beliefs. Neither call reached a deal, but on a deal call this would draft a schedule that breaks the true count. | sim nonsense that slipped the check, exposing a gap in the policy's correction rule |

Why the check misses these: `sim.figures.stance_flipped` flags an `info` draft only when
the rewrite adds both an accept and a reject phrase. A trailing self-negation ("Actually,
it isn't.") or a leading "No," on a correction is neither, so the rewrite is kept. The
contradictory persona's style prompt seems to invite exactly this. Transcripts:
[`s0007_040`](s0007_040_no_fix_contradictory.md),
[`s0007_028`](s0007_028_rescue_contradictory.md).

Genuine NLU misreads that changed an outcome: none found. Haiku's readings in the four
calls above match what the rep's line said.

## Smoke (free, before the paid run)

Command: `--nlu oracle --nlg template --sim-phrasing llm --profile eval --scenarios 5
--seed 7` (`eval_20261009_062152_s7`, thresholds PASS). All 28 sim calls went to Groq
`gpt-oss-120b`.

| | P46a smoke (Gemini → Cerebras) | P46c smoke (Groq, low effort, 512 tokens) |
|---|---|---|
| fallback rate | 0.571 (16/28) | **0.179 (5/28)** |
| empty | 8 | **0** |
| stance | 5 | 4 |
| figures | 3 | 1 |

Three rewritten lines from `s0007_000` (draft kept 0 times in that call):

- "We can do up to 10 payments, with a minimum of $150 each, including balloon payments, and the first payment is due March 31."
- "We can't move on that yet. Still 69%."
- "Sorry, 61% is the lowest we can go."

`empty` was 0, so no second fix was needed.

## Cost (cap $0.25)

| run | Haiku calls | tokens in / out | USD |
|---|---|---|---|
| pilot, 5 scenarios (`eval_20261009_062410_s7`) | 26 | 51,480 / 6,189 | 0.0082 |
| full, 48 scenarios (`eval_20261009_062530_s7`) | 255 | 504,835 / 61,318 | 0.0811 |
| **total** | 281 | 556,315 / 67,507 | **0.0895** |

The budget table (`llm_daily_spend`) recorded 89,511 µ$ for 281 calls, which matches the
audit. The pilot projected $0.08 for 48 scenarios. No other paid calls; the sim, the smoke
and the oracle eval are free.
