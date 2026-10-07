Phase 24b A/B (REVIEW_PLAN §2(c)), run 2026-10-07. Arms on identical seeds and
scenarios: **A** policy + template NLG (today), **B** policy + H3 (ack / answer acts,
3-turn NLG context, bank NLG), **C** ReAct tool-calling agent (eval-only, sees the
private ceiling), **D** LLM-only baseline (eval-only).

Conditions (harder than CI, fixes F8): `--scenarios 48 --seed 7 --profile eval
--nlu llm --sim-phrasing llm --no-oracle-overlay`, two arms at a time with
`providers_split2.yaml` (every per-key rpm / tpm halved). Only scenarios that
**every** arm finished with `status=ok` are compared. Live NLU and LLM sim phrasing
are stochastic, so the same seed does not give the same creditor text in every arm:
arms diverge from the first creditor line on.

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
- Outcome-rate CIs are Wilson 95% intervals; with n ≈ 10 per stratum they are wide.
