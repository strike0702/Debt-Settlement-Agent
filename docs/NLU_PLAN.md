# NLU v2 plan: intent acts and call context (Phases 71a–71b)

## Context

Two live calls on 2026-11-07, both on Claude Haiku:

1. **73 vs 75.** Rep: "seventy five percent" → "I can't go lower than seventy five" → "I think
   seventy three is the lowest I can go". Haiku returned no ask for the last line (stance
   counter; code set `firm` from "lowest I can go"). The policy treated a firm line with no
   number as a repeat of the last final ask and said "Got it, 75% works." Re-asked three times
   in isolation, Haiku read 73 every time; the live call's output was about 550 tokens against
   about 190 in the probe, and the turn took 3.0 s.
2. **Tiers on Haiku** (Phase 69's suggested lines on `tier_definer`): the guided ask came out as
   the "check that figure" fallback (a number word in the template), "the first two payments
   must be at least $190" never joined the tier list, "payments three to five …" was read back
   alone, and "That's all the tiers" set `wants_to_end` and handed the call off. Phase 68's tests
   fed the policy hand-written analyses, never a model's.

Root causes, in the design rather than in single bugs:

- **Flat flags.** One object of about 20 fields, all filled every turn. Flags carry no meaning
  of their own; the policy resolves conflicts by a fixed priority order (`wants_to_end` sits
  above tier collection). Each new situation adds a flag and a place in the order.
- **No call context.** The model sees the line, the agent's last sentence and a pending
  read-back, nothing else: not the phase, the asks and offers on the table, or a list being
  collected.
- **No accounting.** Nothing requires every number the rep said to land somewhere.
- **Long output.** Every field every turn: about 550 output tokens live, the main cost of the
  3 s turn.

## Design

**Acts.** The model returns `{"acts": [...]}`: a short ordered list of typed acts, each with
only its own fields and a verbatim `quote`. One line may carry several acts. The set is closed:

| Group | Types |
|---|---|
| Terms and rules | `answer` (to what the agent asked), `state_rule` (volunteered), `announce_rules` (field, optional count), `rule_item` (list, from/to payment, min amount), `list_done` |
| Price | `price_ask` (pct or total; firmness `opening` / `counter` / `final`), `respond_to_offer` (`accept` / `reject`) |
| Read-backs | `readback_reply` (`confirm` / `deny`, optional correction) |
| Side channels | `question` (topic), `ask_private_info`, `demand_commitment`, `hostility` (level), `end_call` (`goodbye` / `refuse` / `transfer`), `stall`, `backchannel` |
| Fallback | `unclear`: the model cannot map the line; the agent asks the rep to rephrase |

**Call context.** A PUBLIC state summary goes with the line: phase, what the agent is awaiting
(a field, a price response, a read-back, a list item), our last offer, the rep's last ask and
final ask, the list being collected (`tiers`, have / expected), the known creditor rules.
Never the client's maximum, balances, fees or anything PRIVATE (same rule as the NLG prompt;
a test scans the NLU prompt for every private value).

**Code owns numbers.** Code extracts every number in the line and verifies each act's numbers
against its quote (as today). New invariant: a number in the line that no verified act claims
means the turn may not act on older values; the policy asks or reads back instead.

**Coverage.** Every (phase × act type) pair maps to a legal move or a clarifying question, never
a silent drop; a grid test enumerates them (about 8 × 17).

## Phases

| # | Name | Main paths | Depends on |
|---|---|---|---|
| 71a | NLU v2 behind `NLU_SCHEMA=acts`: schema, prompt, verification, call context, number accounting, converter to `TurnAnalysis`, relabelled corpus, scripted Haiku calls, A/B | `app/domain/nlu_types.py` (new act models), `app/agent/nlu/`, `app/llm/prompts.py`, `app/agent/orchestrator.py` (context only), `eval/`, `tests/` | — |
| 71b | Policy reads acts directly (one handler per type), flat flags and priority order removed, grid test, sim emits acts | `app/agent/policy/`, `sim/`, `eval/`, NLU converter removed | 71a merged and adopted |

## Pre-registered adoption rule (71a, written 2026-11-07, before any run)

`NLU_SCHEMA=acts` becomes the default only if, on Claude Haiku 5.5 with the demo settings:

1. Scripted calls (new `eval/scripted_calls`): every scripted turn's expected agent move holds,
   including the 73-vs-75 call, the user's tiers call and the suggested-lines tiers call (the old
   schema fails at least the last two; this rule does not require it to fail).
2. NLU corpus (relabelled, same lines): stance accuracy ≥ old − 0.02; term exact-match (lines
   with terms) ≥ old − 0.02; `ask_client_private_info` recall 1.0; filler false accepts 0.
3. NLU latency p50 on the corpus ≤ old p50 (output tokens reported).
4. Offline oracle eval thresholds PASS, and the oracle path unchanged.

If any fails, the flag stays off and the result is recorded as it is.

## Result of 71a and amended rule (2026-11-07, written after the 71a run)

71a failed the rule above on criterion 2c (private-info recall 39/40: hs27 "what does the client
have left after rent each month" read as a plain question) and regressed on the 14-line amounts
file; it passed the scripted calls 63/63 (flat 49/63) and was faster (p50 1008 vs 1262 ms).
`NLU_SCHEMA` stayed `flat` ([report](eval/nlu_v2_20261107/summary.md)).

**Amendment (user decision D21, made after seeing the result, recorded as such).** For this demo
the priority is that ordinary negotiation turns go smoothly. Private-info recall stops being a
blocker: a missed private-info question cannot leak a private figure, because the reply prompt
never receives PRIVATE facts and `rendered_guard` blocks any private value in a spoken sentence;
the cost of a miss is a generic answer instead of an explicit refusal. It is still reported.

**Rule for 71a-2 (written before its runs).** `NLU_SCHEMA=acts` becomes the default if, on Claude
Haiku 5.5 with the demo settings:

1. Scripted calls: every expected move holds on all scripts, including the new negotiation
   scripts written for 71a-2 before any tuning.
2. Fresh held-out negotiation set (written and frozen before any tuning in 71a-2): stance and
   term exact-match ≥ flat − 0.02 each.
3. Amounts file: term exact-match ≥ flat − 0.02 (after the offer/counter labelling is settled
   and applied to both schemas alike).
4. Main corpus and held-out stance: stance and terms ≥ flat − 0.02; filler false accepts 0.
5. NLU latency p50 ≤ flat; offline oracle eval PASS.

Private-info recall is reported for both schemas but does not block.

## Result of 71a-2 (2026-11-07)

The rule failed on criterion 1: scripted calls acts 91/92, flat 77/92. Both fail held-out
`neg_rule_change` #2, a rule correction after a proposal ("actually … not eight") that is
read as a contradiction, not a revision. Amounts terms failed on the run made under the rule (am14); the
re-run after a fix passed but was not held out. Criteria 2, 4 and 5 pass. `NLU_SCHEMA`
stays `flat` ([report](eval/nlu_v2_20261107_r2/summary.md)).

## Result of 71a-2 and adoption (2026-11-07)

The 71a-2 rule failed as written ([report](eval/nlu_v2_20261107_r2/summary.md)): on scripted calls
acts held 91/92 turns (18/19 scripts) against flat's 77/92 (14/19); its one miss (a rule revision
after a proposal, held-out `neg_rule_change` #2) fails on flat too and comes from a shared code cue.
On amounts, acts dropped the "$900 total" next to "Fifty percent" (am14) in the counted run; the
converter fix came after the result. Every other criterion passed, with acts ≥ flat on the frozen
held-out negotiation set.

**Adopted (user decision D22, second amendment made after a result, recorded as such):**
`NLU_SCHEMA=acts` is the default. The shared revision cue is fixed in 71b.

## Result of 71b (2026-11-08)

The policy reads the verified acts directly (`decide(belief, neg, RepTurn, afford,
context=)`, dispatch order D23); the flat schema, prompt, converter and flags are gone.
On Haiku, scripted calls 92/92 including `neg_rule_change` #2 (a rule change that names
the old value is now a revision). The offline oracle eval decides exactly as before on
every turn. Corpus numbers are within one or two lines of 71a-2's acts run, all from
model variance ([report](eval/nlu_v2_20261108_policy/summary.md)).
