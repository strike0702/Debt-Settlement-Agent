# Phase 71c: fixes from the live call of 2026-11-08, on Claude Haiku 5.5

**Result.** Scripted calls **107/107 turns, 20/20 scripts** (71b: 92/92, 19/19), including
the new `live_20261108_tiers_wrap` (15 turns). Offline oracle eval: thresholds PASS, and
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
  → `scripted_acts.json` here (final run: 105 calls, 194,060 input / 8,069 output tokens,
  NLU p50 1,064 ms).
- Oracle scripted calls: 105/107 (the same 2 `tiers_suggested_lines` turns as main,
  by design).

## The new script

`live_20261108_tiers_wrap` is only partly verbatim. The phase prompt had only an excerpt
and the demo host's audit log could not be read from this session. These rep lines are
verbatim: "eight", the tier 1 line, "I mean, one eighty dollars.", "Let's say it.", "No."
and "No. That's it.". The tier 2 / 3 lines, the read-back yes and the price part are
reconstructed. The live call agreed at 75%; in the script the rep takes our counter,
because that is what reaches WRAP on `tier_definer`. Haiku did not repeat the two-object
reply on "No." in these runs, so the parse fix is pinned by a unit test using the live
reply text.

## Cost

All runs on Haiku: about 0.71M input / 0.03M output tokens (scripted tries, three full
runs, single-script repeats for the sweep line), **about $0.10** of the $1.00 cap.
