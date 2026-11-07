# Phase 71b: the policy reads NLU acts, on Claude Haiku 5.5 (2026-11-08)

**Result.** Scripted calls 92/92 turns, 19/19 scripts, including held-out `neg_rule_change`
#2 (71a-2 acts: 91/92). The offline oracle eval makes the same decisions as main on every
turn of all 100 scenarios. The corpus numbers sit within one or two lines of the 71a-2
acts run; every changed line is model variance (the acts prompt is unchanged, and repeat
runs of the same lines flip between the 71a-2 and 71b readings).

## What changed (for reading the numbers)

- `decide()` takes the verified acts (`RepTurn`) and the call context and reads them in a
  documented order by meaning; the flat schema, its prompt, the acts→flat converter and the
  flat flags are gone ([ADR 10](../../DESIGN.md#adr-10-nlu-v2-acts-and-call-context), D23).
- A rule change after a proposal that names the value it replaces ("six payments max, not
  eight") is a revision: the rule is replaced and the call re-plans (a COUNTER hold here).
- Tier ranges keep their end, so a later tier that leaves a gap or overlaps is asked again
  (`tiers_gap`); the corpus context awaits a read-back when a line has `pending`.
- The acts prompt is byte-identical to 71a-2's (the scripted run's prompt tokens match
  71a-2's to the token: 169,500).

## Setup

- Model `anthropic/claude-haiku-5-5`, pinned `docs/eval/nlu_haiku_20261008/providers_haiku.yaml`,
  profile `claude_haiku_nlu`, `--no-cache` (every call live). Ref date 2026-04-01 (corpus).
- Commands:
  `python -m eval.scripted_calls.run --nlu acts --profile claude_haiku_nlu --providers <file> --out docs/eval/nlu_v2_20261108_policy`;
  `python -m eval.nlu_corpus --label acts_P71B_<file> --corpus <F> --no-cache --profile claude_haiku_nlu --providers <file>`.
- Records: `scripted_acts.json` here; `docs/eval/records/nlu_corpus_acts_p71b_{hneg,main}.jsonl`;
  sections `acts_P71B_hneg` / `acts_P71B_main` in `docs/eval/nlu_corpus.md`. No frozen line,
  label, script or earlier record was edited (the scripted-call oracles were rewritten as acts;
  they only feed `--nlu oracle`).

## Scripted calls (all 19 scripts, 92 turns)

| | 71a-2 acts | 71b |
|---|---|---|
| turns held | 91/92 | **92/92** |
| scripts passed | 18/19 | **19/19** |
| `neg_rule_change` #2 ("…six payments max, not eight.") | CLARIFY 8 vs 6 | **COUNTER hold** (rule now 6) |
| NLU p50 ms | 1099 | 1100 |
| tokens in / out (92 calls) | 169,500 / 7,033 | 169,500 / 7,353 |

## Corpus, against the 71a-2 acts numbers

| file | metric | 71a-2 acts | 71b | lines that moved |
|---|---|---|---|---|
| held-out negotiation (42) | stance | 0.929 | 0.905 | hn37 |
| | terms (n=31) | 0.968 | 0.935 | hn37 |
| | act-type set | 0.976 | 0.952 | hn37 |
| | filler false accepts | 0 | 0 | |
| | NLU p50 ms | 1024 | 1008 | |
| main (193) | stance | 0.907 | 0.896 | n02, f07 (x08 wrong both times) |
| | terms (n=76) | 0.921 | 0.908 | f07, k01 out; rd10 in |
| | act-type set | 0.793 | 0.793 | |
| | private-info recall | 33/34 | 33/34 | f25 both |
| | filler false accepts | 0 | 0 | |
| | NLU p50 ms | 1006 | 1016 | |

Every moved line, with what the model returned (71a-2 → 71b) and a three-run repeat of the
same line on the 71b code:

- **hn37** "Wait, I said fifty five, not fifty." (agent restated 50% at CONFIRM): `price_ask` →
  `readback_reply` only. Five more runs: `price_ask`+`readback_reply` twice, `readback_reply`
  alone three times. With no read-back pending, a bare correction has nowhere to land, so the
  55 is not read (it is not acted on either). Open issue below.
- **n02** "I'm not asking about the client's income, just the percentage.": `ask_private_info`
  (a false positive then) → no act; repeats: none, none, `ask_private_info`.
- **f07** "fine, make it seven payments instead": `state_rule` → `unclear`; repeats: `state_rule`
  three times (with an accept once).
- **k01** "…settle your 100% balance immediately.": a `max_payments` 1 rule appeared; repeats: no
  such rule three times.
- **rd10** (in): `announce_rules` → `state_rule`; repeats: two `state_rule`, one `announce_rules`.
- **x08** "Darn, that's lower than I hoped." (label reject): `stall` → `backchannel`; wrong both times.
- i03, i11, k05 changed act types without changing a scored stance or term; repeats vary too.

## Offline oracle eval (no model)

`python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`:
thresholds PASS; `summary.json` equal to main's in every key except `latency`, and every
per-scenario record (intents, reasons, agent lines, transcript) equal too. The simulator now
emits acts (`RepTurn`) and goes through the same `verify_acts` as the model, with the
model-only readers off. Oracle scripted calls: 90/92 as on main (the two `tiers_suggested_lines`
turns are by design on the oracle path).

## Cost

Haiku only: scripted about 0.17M input / 0.007M output tokens, corpus about 0.41M / 0.013M,
repeat checks about 0.06M / 0.002M. In total about 0.64M input and 0.022M output tokens,
**about $0.08** at $0.10 / $0.50 per Mtok (cap $2.00).

## Open

- hn37 shape: a correction of our restated price with no read-back pending ("I said fifty
  five, not fifty") is often a bare `readback_reply`; read a price correction there as a
  price ask (code or prompt), then re-check on a fresh line.
- x08: a disappointed line ("lower than I hoped") is not read as a reject on either run.
