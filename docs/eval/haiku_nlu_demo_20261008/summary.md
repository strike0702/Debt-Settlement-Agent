# Claude Haiku 5.5 on the demo NLU route (Phase 43, 2026-10-08)

**Decision (user, 2026-10-08):** "Switch demo to Haiku". Apply the Phase 42 v4
NLU prompt, confirm Haiku once more, then make Haiku the demo's first NLU
target in place of Sonnet.

**Outcome: switched.** Every gate check held on the final prompt.

- The v4 prompt is in `app/llm/prompts.py`, plus one sentence (below).
- The `demo` NLU route starts with
  `{target: anthropic/claude-haiku-5-5, params: {output_config: {effort: low}}, budgeted: true}`.
  The free chain after it is unchanged.
- `prices_usd_per_mtok` gained `claude-haiku-5-5: {input: "0.10", output: "0.50"}`.
  The Sonnet price stays, because the eval judge is Sonnet.
- The budget stays at $1 per UTC day.
- **Cost actually spent: $0.138** of the $0.25 cap. All of it was Haiku: $0.132 for
  the eval runs and $0.006 for the live probe. No Sonnet calls were made.

## Confirmation runs

- **Setup:** `docs/eval/nlu_haiku_20261008/providers_haiku.yaml`, profile
  `claude_haiku_nlu`, concurrency 2. The worktree had no response cache, so
  every call was live (0 cache hits, 0 errors). Every run was single model.
- **Runs:**
  1. The v4 patch as is: `HAIKU_P43`, `AMOUNTS_HAIKU_P43`, `HELDOUT_STANCE_HAIKU_P43`.
  2. v4 plus one wording fix (step 3): the `*_P43_FIX` runs. This is the shipped prompt.

### Dropped terms (step 3)

| line | old prompt (P40 / P42 v3) | v4 (P42) | P43 (v4) | P43 fix |
|---|---|---|---|---|
| d10 "Minimum's $75." | kept | dropped | **dropped** | kept |
| t06 "At most two payment levels." | kept | dropped | **dropped** | kept |
| hs09 (held-out, `max_segments`) | kept (before) | dropped | kept | kept |
| am10 "We would need $600 from the client." | ambiguous (P40) | not run | **total** (wrong) | ambiguous |
| d11 "a hundred and ten dollars" | dropped (P40), kept (v3) | kept | kept | dropped |

The d10 and t06 drops came back, and am10 broke the amounts check. Phase 43
allows one general wording fix, so one sentence was added under `info` (no
corpus strings):

```
  Stance never changes extraction: still put every rule or limit the line states
  in terms, and a dollar amount with no total or per-payment cue stays ambiguous.
```

- d11 is a bare spoken amount with no "$". It flips between runs (P40 dropped
  it, v3 and v4 kept it).
- f16, f17 and d04 are wrong on every Haiku run.

### Switch gate (step 4), final prompt

| check | needed | Haiku P43 fix | result |
|---|---|---|---|
| main stance accuracy | ≥ 0.820 | **0.869** | pass |
| held-out stance accuracy | ≥ 0.85 | **0.969** | pass |
| main private-info recall | 1.000 | **1.000** (34/34) | pass |
| held-out private-info recall | ≥ 0.833 | **1.000** (6/6) | pass |
| amounts terms correct, am09 ambiguous | 14/14 | **14/14** (am09 → `amount_ambiguous` $420) | pass |
| main term exact-match, lines with terms | ≥ 60/66 | **62/66** | pass |
| filler false accepts | 0 | **0** (of 31) | pass |

### All numbers (main corpus, 183 lines)

| | Haiku P40 (old prompt) | Haiku P42 v4 | Haiku P43 (v4) | **Haiku P43 fix** | Sonnet P39 (old prompt, Phase 41 demo) |
|---|---|---|---|---|---|
| stance accuracy | 0.601 | 0.896 | 0.891 | **0.869** | 0.820 |
| private info P / R | 0.941 / 0.941 | 0.944 / 1.000 | 0.944 / 1.000 | **0.971 / 1.000** | 0.971 / 1.000 |
| commitment P / R | 0.765 / 1.000 | 0.722 / 1.000 | 0.684 / 1.000 | **0.722 / 1.000** | 0.867 / 1.000 |
| accept P / R | 0.846 / 1.000 | 0.917 / 1.000 | 0.917 / 1.000 | **0.917 / 1.000** | 0.917 / 1.000 |
| reject P / R | 0.692 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 | **1.000 / 1.000** | 0.900 / 1.000 |
| terms exact, lines with terms (66) | 62 | 60 | 61 | **62** | 63 |
| filler false accepts (31) | 1 | 0 | 0 | **0** | 1 |

- **Run-to-run variance (v4: P42 vs P43, same prompt):** 6 field changes.
  - stance: c18, k04, k06;
  - terms: i06;
  - flags: x02 `wants_to_end`, k04 `demands_commitment`.
  - Stance accuracy moved by 0.005. These two runs are the noise floor for one-line moves.
- **Fix vs v4 (P43):** stance is 0.022 lower (4 lines net).
  - Four counters became `offer`: f01, t08, t13, t14. The corpus labels a rep's
    ask in reply to the agent `counter`, and policy treats offer and counter the same.
  - k05 became `counter`; e10 and f14 became `stall`.
  - c08, c14, c18 and k01 were fixed.
  - n14 (private-info FP) and k04 (commitment FP) are gone.

### Held-out stance set (32 lines) and amounts (14 lines)

| | Haiku before (P42) | Haiku P42 v4 | Haiku P43 (v4) | **Haiku P43 fix** |
|---|---|---|---|---|
| held-out stance accuracy | 0.562 | 0.906 | 0.938 | **0.969** |
| held-out private info P / R | 0.750 / 1.000 | 1.000 / 0.833 | 1.000 / 0.833 | **1.000 / 1.000** |
| held-out terms exact, lines with terms (16) | 15 | 14 | 15 | **15** (hs08 `even` dropped, as always) |
| amounts terms correct (14) | 14 (P40) | not run | 13 (am10) | **14** |
| amounts stance accuracy | 0.714 (P40) | not run | 0.571 | 0.429 |

- **Amounts stance:** the ambiguous-amount lines am09, am10 and am11 are now
  `info`, and asks are `counter` where this set expects `offer` (Phase 42
  open issue (d)).
  - The "total or per payment?" clarify fires on the NLU's ambiguous-amount
    flag, not on the stance (`app/agent/nlu.py`, `resolve_amounts` trigger
    `nlu_flag`), so the agent still asks.
  - Sonnet P39 scored 0.571 on this set.
- **Held-out:** only miss is hs20 `wants_to_end` (stance right).

## Live probe on the shipped demo route (step 6)

**Command:** `uv run python scripts/claude_nlu_probe.py --db <scratch>.db --cap-usd 0.10`

- 20 lines of `tests/nlu_corpus.jsonl` (every 9th) through `app.agent.nlu.analyze`
  on the shipped `demo` profile, with the response cache off.
- The probe gained `--model`. By default it reports the demo route's budgeted
  target and stops if `--model` names anything else.

| measure | Haiku 5.5 (this probe) | Sonnet 5.5 (Phase 41 probe) |
|---|---|---|
| NLU calls | 20 ok, 0 failed over, 0 timeouts | 20 ok |
| request latency (audit `latency_ms`) | p50 **1172 ms**, p95 **1805 ms**, max 2279 ms | p50 2068 ms, p95 2701 ms |
| tokens per call | in mean 1950, out mean 194 (max 418) | in 1372, out 186 |
| cost per call | mean **$0.00029**, max $0.00040 | $0.0046 |
| spent | $0.0058 | $0.092 |

- **Budget:** at $0.00029 per call, $1 per UTC day covers about 3,400 rep
  turns. On Sonnet it covered about 215.
- **Eval-run latency:** across the 422 calls of the four main-corpus and
  held-out runs, Haiku p50 was 1217 ms and p95 1838 ms. Groq text-turn NLU
  is 1415 / 2215 ms.
- **Forced exhaustion** (same route, budget $0):
  - wrote `llm_budget_exhausted` and `llm_budget_skip` at 0 ms, with no Anthropic request;
  - then answered from `groq/openai/gpt-oss-120b` with
    `failover_from = anthropic/claude-haiku-5-5` in 2022 ms.

## Cost actually spent (audit `eval/results/p43_audit.db` + probe budget DB)

| run | calls | input | output | cost |
|---|---|---|---|---|
| AMOUNTS_HAIKU_P43 | 14 | 26,920 | 3,713 | $0.0046 |
| HAIKU_P43 | 179 | 344,182 | 34,608 | $0.0517 |
| HELDOUT_STANCE_HAIKU_P43 | 32 | 61,509 | 6,210 | $0.0093 |
| AMOUNTS_HAIKU_P43_FIX | 14 | 27,676 | 3,605 | $0.0046 |
| HAIKU_P43_FIX | 179 | 353,848 | 35,030 | $0.0529 |
| HELDOUT_STANCE_HAIKU_P43_FIX | 32 | 63,237 | 6,060 | $0.0094 |
| live probe (shipped route) | 20 | 38,992 | 3,878 | $0.0058 |
| **total** | 470 | 916,364 | 93,104 | **$0.138** |

- Prices: $0.10 per million input tokens, $0.50 per million output tokens.
- The forced-exhaustion call went to Groq (free tier).
- The first launch of the fix runs failed before any call (a zsh loop did not
  split its arguments), so nothing was spent on it.

## Not measured

- **Groq on the new prompt:** Groq is the free fallback after the cap and has
  not been measured on this prompt. A free-tier rerun is owed.
  - The eval tools skip budgeted targets by default, so
    `python -m eval.nlu_corpus --label GROQ_P43` on the `demo` profile runs
    the free chain only.
  - `--allow-budgeted` keeps the paid target.
- **Sonnet on the new prompt:** not measured. It is no longer on the demo
  route; it remains the eval judge.

## Raw probe output

```text
  p01 anthropic/claude-haiku-5-5    1808 ms  stance=question  errors=0
  p10 anthropic/claude-haiku-5-5    1645 ms  stance=question  errors=0
  p19 anthropic/claude-haiku-5-5    1140 ms  stance=question  errors=0
pilot: spent $0.000849, projected $0.00566
  p28 anthropic/claude-haiku-5-5    1137 ms  stance=question  errors=0
  n07 anthropic/claude-haiku-5-5    1361 ms  stance=info  errors=0
  n16 anthropic/claude-haiku-5-5    1124 ms  stance=question  errors=0
  c07 anthropic/claude-haiku-5-5    1092 ms  stance=question  errors=0
  c16 anthropic/claude-haiku-5-5    1230 ms  stance=info  errors=0
  i07 anthropic/claude-haiku-5-5    1304 ms  stance=other  errors=0
  h05 anthropic/claude-haiku-5-5    2282 ms  stance=info  errors=0
  f04 anthropic/claude-haiku-5-5    1192 ms  stance=info  errors=0
  f13 anthropic/claude-haiku-5-5    1091 ms  stance=stall  errors=0
  f22 anthropic/claude-haiku-5-5    1474 ms  stance=info  errors=0
  d01 anthropic/claude-haiku-5-5    1158 ms  stance=other  errors=0
  d10 anthropic/claude-haiku-5-5    1227 ms  stance=info  errors=0
  t08 anthropic/claude-haiku-5-5    1135 ms  stance=offer  errors=0
  a02 anthropic/claude-haiku-5-5    1562 ms  stance=accept  errors=0
  a11 anthropic/claude-haiku-5-5    1152 ms  stance=offer  errors=0
  x04 anthropic/claude-haiku-5-5    1079 ms  stance=other  errors=0
  e05 anthropic/claude-haiku-5-5    1145 ms  stance=other  errors=0

--- claude-haiku-5-5 NLU (demo route, effort low, 6 s NLU timeout) ---
calls ok=20 failed=0 lines=20
latency_ms p50=1172 p95=1805 max=2279
tokens in mean=1950 out mean=194 out max=418
cost per call mean=$0.000292 max=$0.000404
budget DB: spent $0.005846 of $0.1 (2026-10-08)

--- forced budget-exhausted call ---
  llm_budget_exhausted: anthropic/claude-haiku-5-5 failover_from=None 0 ms
  llm_budget_skip: anthropic/claude-haiku-5-5 failover_from=None 0 ms
  ok: groq/openai/gpt-oss-120b failover_from=anthropic/claude-haiku-5-5 2022 ms
  stance=question
```
