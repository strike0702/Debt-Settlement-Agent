# Held-out negotiation corpus (Phase 71a-2)

`nlu_corpus_heldout_negotiation.jsonl`: 42 fresh lines of ordinary negotiation a
collections rep says, written for the 71a-2 adoption rule (`docs/NLU_PLAN.md`) and
**frozen before any 71a-2 change to code or prompts** (commit
`phase 71a-2: frozen held-out negotiation set and scripts`). Do not edit it; a label
found wrong later is listed in the phase report, not fixed here.

## Offer vs counter (settled first, applies to every corpus file and both schemas)

A rep line that names a settlement price (a percent of the balance or a dollar total
to settle) is **`offer` only when the agent has not spoken yet** (`Agent last said` is
`(opening)`, i.e. the rep opens the call with a price). **Every other price line is
`counter`**: the first price after the agent's question, a dollar total, a haggling
step, a floor. This is the definition the flat prompt already gives ("offer: the
same, but only when Agent last said is (opening)") and the acts converter already
uses (`opening = not last_agent_line`). The policy treats `offer` and `counter` the
same, so the label only has to be consistent; no corpus line here is an opening.

## Other label conventions (same as the main corpus)

- `ask_pct` / `ask_total_cents` carry the rep's ask; a self-correction labels only the
  corrected number ("around thirty... no, sorry, seventy five" → 75).
- `firm` only for a floor / final / cannot-go-lower statement (the code `_FIRM_RE` set).
- A rejection that echoes our number ("Fifty? No way") is `reject` with no ask.
- Read-back corrections use `context` (the agent's read-back) and `pending` (the field);
  the correction is the field's term, stance `info` (as the read-back fast path gives).
- `acts` lists the expected act types (scored as a set, `answer` / `state_rule` /
  `rule_item` merged). Lines tagged `filler` count toward filler false accepts.
- Agent aliases: `default` (discovery), `counter` (we offered 50%), `confirm` (our
  schedule read-out), `min_ask` (the minimum-payment question). Ref date 2026-04-01.
