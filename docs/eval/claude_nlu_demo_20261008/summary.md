# Claude Sonnet 5.5 on the demo NLU route: live probe (Phase 41, 2026-10-08)

The demo profile's NLU route is now Claude Sonnet 5.5 (effort `low`, the 6 s NLU
role timeout), then the unchanged free chain, under a daily budget
(`CLAUDE_DAILY_BUDGET_USD`, default $1.00 per UTC day). This probe checks speed,
cost and the budget fallback on the shipped route.

**Command:** `uv run python scripts/claude_nlu_probe.py --db <scratch>.db`
(20 lines of `tests/nlu_corpus.jsonl`, every 9th line, through
`app.agent.nlu.analyze`, response cache off, probe budget capped at $0.25).

## Result

| measure | value |
|---|---|
| Sonnet NLU calls | 20 ok, 0 failed over, 0 timeouts |
| request latency (audit `latency_ms`) | p50 **2068 ms**, p95 **2701 ms**, max 3207 ms |
| tokens per call | input mean 1372, output mean 186 (max 213) |
| cost per call | mean **$0.0046**, max $0.0049 |
| spent (this probe) | **$0.092** (counted by the same `DailyBudget` the demo uses) |

At $0.0046 per NLU call, $1.00 covers about 215 rep turns per UTC day. Once the
day's spend reaches the cap, NLU runs on Groq until 00:00 UTC.

**Forced exhaustion** (same route, budget $0): the call wrote
`llm_budget_exhausted` and `llm_budget_skip` (0 ms, no Anthropic request), then
answered from `groq/openai/gpt-oss-120b` with `failover_from =
anthropic/claude-sonnet-5-5` in 1718 ms.

Groq for comparison: text-turn NLU 1415 ms p50 / 2215 ms p95 after the key pool
([latency_20261007.md](../latency_20261007.md#after-key-pool-phase-27-2026-10-07)).
Sonnet adds about 0.6 s p50, and p95 stays well inside the 6 s timeout.

## Raw output

```text
  p01 anthropic/claude-sonnet-5-5    2703 ms  stance=question  errors=0
  p10 anthropic/claude-sonnet-5-5    2106 ms  stance=other  errors=0
  p19 anthropic/claude-sonnet-5-5    1943 ms  stance=question  errors=0
pilot: spent $0.013504, projected $0.090026
  p28 anthropic/claude-sonnet-5-5    1613 ms  stance=question  errors=0
  n07 anthropic/claude-sonnet-5-5    2266 ms  stance=offer  errors=0
  n16 anthropic/claude-sonnet-5-5    2502 ms  stance=question  errors=0
  c07 anthropic/claude-sonnet-5-5    1980 ms  stance=question  errors=0
  c16 anthropic/claude-sonnet-5-5    1881 ms  stance=info  errors=0
  i07 anthropic/claude-sonnet-5-5    2600 ms  stance=other  errors=0
  h05 anthropic/claude-sonnet-5-5    2070 ms  stance=info  errors=0
  f04 anthropic/claude-sonnet-5-5    2072 ms  stance=counter  errors=0
  f13 anthropic/claude-sonnet-5-5    1626 ms  stance=stall  errors=0
  f22 anthropic/claude-sonnet-5-5    1844 ms  stance=info  errors=0
  d01 anthropic/claude-sonnet-5-5    1979 ms  stance=info  errors=0
  d10 anthropic/claude-sonnet-5-5    2209 ms  stance=info  errors=0
  t08 anthropic/claude-sonnet-5-5    2036 ms  stance=counter  errors=0
  a02 anthropic/claude-sonnet-5-5    3212 ms  stance=accept  errors=0
  a11 anthropic/claude-sonnet-5-5    2213 ms  stance=offer  errors=0
  x04 anthropic/claude-sonnet-5-5    1939 ms  stance=other  errors=0
  e05 anthropic/claude-sonnet-5-5    2099 ms  stance=stall  errors=0

--- Sonnet NLU (demo route, effort low, 6 s NLU timeout) ---
calls ok=20 failed=0 lines=20
latency_ms p50=2068 p95=2701 max=3207
tokens in mean=1372 out mean=186 out max=213
cost per call mean=$0.004599 max=$0.004874
budget DB: spent $0.091984 of $0.25 (2026-10-08)

--- forced budget-exhausted call ---
  llm_budget_exhausted: anthropic/claude-sonnet-5-5 failover_from=None 0 ms
  llm_budget_skip: anthropic/claude-sonnet-5-5 failover_from=None 0 ms
  ok: groq/openai/gpt-oss-120b failover_from=anthropic/claude-sonnet-5-5 1718 ms
  stance=question
```
