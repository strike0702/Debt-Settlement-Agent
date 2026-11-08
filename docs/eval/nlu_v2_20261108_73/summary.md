# Phase 73: repeat requests and the natural schedule read-out, on Claude Haiku 5.5

**Result.** Scripted calls **124/124 turns, 23/23 scripts** on the final code, including the
three new scripts (`scripted_acts.json`). The 12 new corpus lines: all 8 repeat phrasings read
as `repeat_request`, and none of the 4 near-misses did. Offline oracle eval: thresholds PASS,
`summary.json` matches main's except latency, and per-scenario decisions match main's. Only
the agent's wording differs (the opening line varies by run id).

## What Haiku returned before this phase

The old prompt (no `repeat_request`), run twice per line with a reduced context:

| Line | Act returned | Runs |
|---|---|---|
| "Sorry. Can you come again?" | `stall` | 2 of 2 |
| "What did you propose last" | `question` topic `other` | 2 of 2 |

The live call spoke the `unclear` line for "come again", so the reading is not stable or
depends on the full live context. Either way the agent had no path to repeat itself.

## What changed

- **Act.** `repeat_request`, with one line and one example in the prompt: **+57 prompt tokens
  per call** (1708 → 1765, measured on one call each).
- **Code cue.** It adds the act when the whole line is a repeat phrase, so it does not depend
  on the model. A line with a rule, a price or more words is not a repeat.
- **Policy.** The agent says its last move again from the same facts, with a "Sure." lead. A
  second repeat in a row gives the short form. Nothing else changes: no ladder step, no
  phase change, and no no-progress or question count.
- **Schedule read-out.** One natural sentence, with runs of equal amounts grouped. Long
  schedules are summarised, and every form states the payment count.

From the live transcript, on Haiku:

```
Rep:   I'm looking at eighty five percent.
Agent: We can propose 52% of the balance, which is $650. Would that work?
Rep:   Sorry. Can you come again?
Agent: Sure. We can propose 52% of the balance, which is $650. Would that work?
Rep:   What did you propose last
Agent: Our offer is 52% of the balance, which is $650.
Rep:   Can we do it at seventy five percent?
Agent: We can propose 57% of the balance, which is $712.50. Would that work?
```

## Two things the runs found

1. **The new prompt lines broke an older turn.** `live_20261108_tiers_wrap` #6 ("I mean, one
   eighty dollars.", the answer to a tier clarify) was read as a tier in 8 of 8 probes with the
   first, three-line wording; the old prompt gave a mix of minimum and tier readings. A one-line
   wording improved the probes (2 of 10 misread), but the full script still failed on that turn
   in both runs. Code now reads a single dollar amount as the tier's minimum while that clarify
   is open (`acts._tier_clarify_minimum`). A first version failed 1 of 2 runs because it
   stood aside when the model returned any tiers term; after that fix the script passed 6 of 6
   runs. It was not in the phase list; the all-turns criterion needed it.
2. **The first read-back script depended on the model.** "I think maybe around eight payments"
   was not always read as hedged, so `repeat_read_back` sometimes had no read-back to repeat.
   It now uses a tier read-back, which code decides; it passed 3 of 3 runs alone and in the
   final full run. The hedge reading itself is listed as an open issue.

## Near-misses

| Line | Read as |
|---|---|
| "yeah so the the um with the uh" | `stall` (labelled `unclear`; not a repeat, so the agent never re-says on it) |
| "What happens after the client approves?" | `question` |
| "Why can't you go any higher?" | `question` |
| "Who decides on your side?" | `question` |

Act-type set on the 12 lines: 11 of 12 (0.917) on the final prompt. The one miss is the first
row above. The same wording before a line wrap scored 12 of 12, so r09 varies between runs.

## Not measured

The full main corpus was not re-run; only the 12 new lines were. The phase asked for those, and
the scripted calls cover the wider behaviour.

**Paid calls.** Haiku only: 7 full scripted runs plus the probes and corpus runs, about 1.7M
input and 0.06M output tokens. That is **about $0.25** (estimated at 71c's rate), under the
$1.00 cap.
