# NLU v2 (acts + call context) vs the flat schema on Claude Haiku 5.5 (Phase 71a, 2026-11-07)

**Result: the adoption rule fails (criterion 2), so `NLU_SCHEMA` stays `flat`.**
Acts passes the scripted calls (63/63 turns vs 49/63), latency (p50 1008 ms vs
1262 ms) and the oracle check. On the corpus it misses one private-info line
(39/40, the rule needs 40/40) and drops on the 14-line amounts file.

## Setup

- Model: `anthropic/claude-haiku-5-5`, effort low, one target with no fallback:
  `docs/eval/nlu_haiku_20261008/providers_haiku.yaml`, profile `claude_haiku_nlu`
  (the demo NLU route's model and params).
- Corpus: `tests/nlu_corpus.jsonl` (193), `tests/nlu_corpus_amounts.jsonl` (14),
  `tests/nlu_corpus_heldout_stance.jsonl` (32). Same lines and labels for both schemas.
  `acts` labels were added (`acts` key; 39 hand-fixed, listed in `docs/history/PROGRESS.md`).
  `--no-cache`: every call live, so latency is comparable. Ref date 2026-04-01.
- Acts context per corpus line comes from its `agent` alias (`eval.nlu_corpus.line_call_context`).
- Scripted calls: `eval/scripted_calls/scripts/` (11 scripts, 63 rep turns; the
  Phase 69 operator-rule script is skipped until 69 merges). Records:
  `scripted_flat.json`, `scripted_acts.json` here.
- Commands:
  `python -m eval.nlu_corpus --label <L> [--corpus F] --schema flat|acts --no-cache --profile claude_haiku_nlu --providers <file>`;
  `python -m eval.scripted_calls.run --nlu flat|acts --profile claude_haiku_nlu --providers <file> --out docs/eval/nlu_v2_20261107`.

## The rule (docs/NLU_PLAN.md, written before any run), criterion by criterion

| # | Criterion | Flat | Acts | Pass? |
|---|---|---|---|---|
| 1 | Scripted calls: every expected move holds (incl. 73-vs-75, user tiers, suggested-lines tiers) | 49/63 turns, 7/11 scripts | **63/63 turns, 11/11 scripts** | **PASS** |
| 2a | Stance accuracy ≥ flat − 0.02 (all three files, pooled) | 214/239 = 0.895 | 210/239 = 0.879 (bar 0.875) | PASS pooled; per file: main PASS, held-out PASS, amounts FAIL (0.357 vs 0.571) |
| 2b | Term exact-match, lines with terms ≥ flat − 0.02 | 96/105 = 0.914 | 96/105 = 0.914 | PASS pooled; per file: main PASS, held-out PASS, amounts FAIL (0.769 vs 1.000) |
| 2c | `ask_client_private_info` recall = 1.0 | 40/40 | **39/40** (miss hs27) | **FAIL** |
| 2d | Filler false accepts = 0 | 0 | 0 | PASS |
| 3 | NLU latency p50 ≤ flat p50 (output tokens reported) | 1262 ms, 213 out tokens/line | 1008 ms, 65 out tokens/line | PASS |
| 4 | Offline oracle eval thresholds PASS, oracle path unchanged | — | PASS; 639 turns, `turns_to_proposal` 6.714, `turns_to_outcome` 5.390 (identical to Phase 68) | PASS |

2c fails whichever way the files are combined, so the rule fails. The rule does not say whether
2a and 2b are pooled or per file; both readings are shown above.

## Per file

| File | Schema | Stance | Terms (with terms) | Private recall | p50 / p95 ms | Out tokens / line |
|---|---|---|---|---|---|---|
| main (193) | flat `FLAT_P71A_FINAL` | 0.912 | 0.908 (76) | 34/34 | 1264 / 1613 | 210 |
| main (193) | acts `ACTS_P71A_FINAL` | 0.902 | 0.921 (76) | 34/34 | 1010 / 1543 | 60 |
| amounts (14) | flat | 0.571 | 1.000 (13) | — | 1302 / 3132 | 265 |
| amounts (14) | acts | 0.357 | 0.769 (13) | — | 1056 / 3059 | 143 |
| held-out stance (32) | flat | 0.938 | 0.875 (16) | 6/6 | 1249 / 1554 | 205 |
| held-out stance (32) | acts | 0.969 | 1.000 (16) | 5/6 | 988 / 1657 | 63 |

Act-type set exact (acts only, main corpus): see `ACTS_P71A_FINAL` in `docs/eval/nlu_corpus.md`.
Prompt tokens per line: about 2.1K flat vs 1.6K acts (the context block included).

## What acts got wrong

- **hs27** "Roughly what does the client have left after rent each month?": read as a
  `question`, not `ask_private_info`; the code-side private cue does not match "left
  after rent" either. One line, but the rule requires full recall.
- **Amounts**: a dollar total is `counter`, not `offer`. The converter uses the flat
  prompt's definition (an offer only in reply to our opening), but these lines are
  labelled `offer` (flat got 0.571 here too). am09 (the long $420 line) came back
  `unclear`. am11 "Three payments, $350." was read as a minimum payment, not ambiguous.
  am14 lost the $900 total next to the 50%.

## How the acts prompt and converter were tuned (read this before trusting the main-corpus row)

The acts path was run four times on the main corpus before the final run
(`ACTS_P71A`, `_R2`, `_R3`, `_R4` in `docs/eval/nlu_corpus.md`; stance 0.777 → 0.860 →
0.870 → 0.886). Each round fixed mapping gaps seen on that corpus: a statement with no
act scores as `info`, offer vs counter, an announcement without a count, the full
private-info definition, change requests as `counter`, a price act that does not verify,
and yes / no never `unclear`. The main-corpus row is therefore not held out. The amounts
and held-out stance files were run once, after the code was frozen. The scripted calls
were run once on flat. They were run twice on acts: the first run (60/63) led to two
code fixes (a yes / no is never `unclear`; a bare "three to five … $250" tier range),
and then the final 63/63 run.

## Cost

All Phase 71a paid calls (smoke, four tuning rounds, final A/B, scripted calls):
about 2.79M input and 0.17M output tokens, **about $0.37** at $0.10 / $0.50 per Mtok
(cap $3.00).

## Decision

The flag stays `flat` (the default in `app/config.py`). The suggested next steps are in
`docs/history/PROGRESS.md` (Phase 71a open issues): fix the private-info miss in the prompt
and the cue, settle the offer / counter label rule for dollar totals, then re-run the A/B
under the same rule.
