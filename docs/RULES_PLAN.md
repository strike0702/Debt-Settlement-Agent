# Creditor rules plan (Phases 68–69)

## Context

Live voice call, 2026-11-07: the rep said "before that, I need to define the payment tiers" and
"we will have three payment tiers"; the agent asked for the settlement percentage three times.
The agent already has fields for tiers, payment levels (`max_segments`), token payments and
the first payment date, and the NLU reads tiers stated in full. But it asks only the three
required rules and assumes the optional ones (no tiers, 2 levels, no token limit) until the
final read-back. An announcement without values extracts nothing, so the policy falls
through to its next missing item.

## Phases

| # | Name | Main paths | Depends on |
|---|---|---|---|
| 68 | The rep can define rules: `defines_field` cue, yield and guided ask, tiers over several turns with one read-back, optional sweep for flexible schedules, `tier_definer` scenario | `app/agent/nlu/`, `app/agent/policy/`, NLG bank, `sim/`, `fixtures/scenarios/` | — |
| 69 | Operator-entered known rules (UI, manual review): "Known creditor rules" panel, `start.known_rules`, belief seeded as KNOWN from `operator`, per-creditor defaults, "Fill from scenario" | `web/src/`, `app/voice/ws.py`, `app/schemas/events.py`, `app/domain/belief.py`, fixtures firm JSON | — |

**Execution:** 68 ∥ 69 in separate worktrees; the file sets are disjoint (68 must not touch
belief / ws / events / web; 69 must not touch NLU / cascade / moves / sim / bank). 69 waits for
the user's browser review. Merge order: whichever finishes first; the second merges main
before its gates.

**Why both.** 68 fixes the conversation the user had. 69 matches how settlement firms work:
they usually know a creditor's rules before dialling, and it gives a reliable way to demo
tiers without saying them perfectly by voice.

Prompts: `../dsa-orch-tools/prompt-p68.txt`, `prompt-p69.txt` (standard worktree header,
DEFERRED lines, lean testing, no paid calls).
