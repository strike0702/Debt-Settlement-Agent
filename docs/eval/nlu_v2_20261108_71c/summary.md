# Phase 71c: fixes from the live call of 2026-11-08, on Claude Haiku 5.5

**Result.** Scripted calls **109/109 turns, 20/20 scripts** (71b: 92/92, 19/19), including
the new `live_20261108_tiers_wrap` (17 turns, every rep line verbatim). Offline oracle eval: thresholds PASS, and
`summary.json` matches main's except for latency. Per-scenario records match on every
intent, outcome and price. The only other differences are timings and one opening line per
call, whose wording varies by run id.

## What changed

- **Last JSON wins.** When a reply holds several JSON objects (the model corrected
  itself), the last one that validates is used. The audit row `nlu_acts_invalid` records
  `used object k of n`. The live reply (a reject, then "Wait, that's not right", then
  `{"acts":[]}`) now reads as no acts.
- **Prompt.** `Reply with JSON only:` became `Reply with one JSON object only:` (+2 prompt
  tokens per call, measured: 9,190 → 9,200 over 5 calls). A first wording, `exactly one
  JSON object and nothing else`, on its own line, made Haiku read the sweep reply "No,
  nothing like that." as `min_payment_tiers: []` on both runs of the full set. Repeat runs
  showed that reading is flaky with either wording, so the code now drops it (next bullet).
- **Empty tiers after the sweep.** A model `min_payment_tiers: []` given in reply to the
  optional-rules sweep is dropped, so the sweep's own "no" path handles it and the empty
  list is not read back. A stated "there are no tiered minimums" elsewhere is unchanged, and
  so are oracle acts.
- **WRAP never reopens.** After the proposal the moves are: CLOSE, read the schedule, ask
  again on an unreadable line, the safety refusals, or a hand-off
  (`changed_after_proposal`, which also clears the drafted agreement) for a real change.
  A real change is a new price, a rule value that differs from belief, tiers, or an
  announced rule. "No", a reject, an empty act list or a goodbye closes. The old
  `wrap_renegotiate` re-entry into CONFIRM / NEGOTIATE is gone.
- **Tier clarify answer.** The `tiers_inconsistent` clarify keeps the tier it took out
  (`NegotiationState.tier_pending`). A minimum in the reply ("I mean, one eighty dollars."
  read as `min_payment_cents`) completes that tier with its range, and the agent asks for
  the next tier instead of restarting at tier 1.

## Setup

- Model `anthropic/claude-haiku-5-5`, pinned `docs/eval/nlu_haiku_20261008/providers_haiku.yaml`,
  profile `claude_haiku_nlu`.
- `python -m eval.scripted_calls.run --nlu acts --profile claude_haiku_nlu --providers <file> --out docs/eval/nlu_v2_20261108_71c`
  → `scripted_acts.json` here (final run: 106 calls, 196,069 input / 8,829 output tokens,
  NLU p50 1,068 ms).
- Oracle scripted calls: 107/109 (the same 2 `tiers_suggested_lines` turns as main,
  by design).

## The new script

`live_20261108_tiers_wrap` is the live call: every rep line is verbatim and in order, on
`easy_deal` (our first counter "52% of the balance, or $650" matches it). Expected moves:

- "I mean, one eighty dollars." completes tier 1 (payments 1-2, at least $180) and asks
  for the next tier. Live, the agent restarted at tier 1.
- The rep's next live line repeats tier 1 ("It starts from payment one, ... one eighty
  dollars."). It replaces the same tier, adds no duplicate, and the next tier is asked
  again. The read-back still says "1 to 2 at least $180".
- "No." after "anything else?" closes (`post_wrap`). Live, the agent asked for the first
  payment date. "That's it." then gets `already_ended`.
- Every other turn pins what the live agent did: the rephrased ask after "Let's say it.",
  the read-back, the counters at 52% and 63%, the confirm at 75%, and the proposal.

Haiku did not repeat the two-object reply on "No." in these runs, so the parse fix is
pinned by a unit test using the live reply text.

## Cost

All runs on Haiku: about 0.93M input / 0.04M output tokens (scripted tries, four full
runs, single-script repeats for the sweep line and the verbatim script), **about $0.13** of
the $1.00 cap.
