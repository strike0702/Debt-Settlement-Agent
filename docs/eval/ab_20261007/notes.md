Phase 24b A/B (REVIEW_PLAN §2(c)), run 2026-10-07 (arms A, B, D) and 2026-10-08
(arm C finished). Arms on identical seeds and scenarios: **A** policy + template NLG
(today), **B** policy + H3 (ack / answer acts, 3-turn NLG context, bank NLG), **C**
ReAct tool-calling agent (eval-only, sees the private ceiling), **D** LLM-only
baseline (eval-only; dropped at 13/48, reported separately below the decision).

Conditions (harder than CI, fixes F8): `--scenarios 48 --seed 7 --profile eval
--nlu llm --sim-phrasing llm --no-oracle-overlay`, at most two arms at a time with
`providers_split2.yaml` (every per-key rpm / tpm halved). Only scenarios that
**every** arm finished with `status=ok` are compared. Live NLU and LLM sim phrasing
are stochastic, so the same seed does not give the same creditor text in every arm:
arms diverge from the first creditor line on. Arm C ran over two days because the
free-tier daily quotas ran out (Gemini per-day requests, Groq tokens per day);
`--resume` re-ran only the `skipped_quota` scenarios, with the same providers file.

Naturalness judge: `eval.judge_naturalness` with the `judge` role (Claude Sonnet
5.5, effort low, no fallback; Phase 30, run from a detached checkout of main @
`bb9b636` because this branch predates the role), blind, each pair judged twice with the order
swapped; a pair counts as a win only when both orders agree, otherwise a tie. Three
judges over 48 pairs each (288 calls, plus a 2-pair smoke) cost about $1.17 (≈ 429k input, 31k output
tokens incl. thinking). Win-rates are over decisive pairs with Wilson 95% CIs; the
judge is a secondary metric, not a gate on its own.

Reading the numbers:

- **The simulated creditor reacts to intents and facts, not to text** (carry-over
  24a.3; `sim/creditor.py` `_decide`). A spoken figure that differs from the move's
  facts never changes the creditor's reply. So the outcome rates, surplus and
  `agreement_valid` of C and D (whose LLM writes its own numbers) measure their
  move choice, not what they said; a wrong spoken number is caught only by the
  `leaks` / `unverified` columns. The naturalness judge reads the text, so it does
  see wording, but not whether a real creditor would have reacted to it.
- `agreement_valid` < 1 in A and B comes from live-NLU extraction errors: the deal
  was confirmed on rules the NLU read wrongly, so the schedule breaks the creditor's
  true rules (e.g. `s0007_015` fails the same way in both arms). The oracle CI eval
  (`--nlu oracle`) has `agreement_valid = 1`.
- B's one leak (`s0007_006`) is a **policy** ACCEPT move, not an H3 act: the LLM
  sim invented "settle the 100% balance", live NLU read it as the creditor's ask,
  and the unchanged policy accepted it aloud; 100% equals that client's true
  ceiling, so the metric counts it. No ack or answer act spoke a private figure.
- C's 2 leaks (`s0007_026`, `s0007_044`) and 8 unverified figures (7 scenarios) are
  spoken by the ReAct agent's own text; C also escalates far less often than the
  policy when it should (0.22 vs 1.00).
- **ReAct tool calls are prompt-based JSON, not native provider function calling**
  (carry-over 24a.2 / 26.1). On the final 48 scenarios arm C made 588 steps: 70
  `step_invalid` (unparseable JSON, 11.9%) and 15 `step_rejected` (a valid call the
  move builder refused, 2.6%), 14.5% invalid in all (it was ~13% on the first 13
  scenarios). Native function calling would trim most of the 70 retry calls, but C
  makes 2.45 LLM calls per turn against A's 0.88 and its p50 server time is ~5× A's,
  so it would not close the cost or latency gap with the policy.
- Outcome-rate CIs are Wilson 95% intervals; with n ≈ 10 per stratum they are wide.
