# NLU v2 (acts) vs flat on Claude Haiku 5.5, Phase 71a-2 re-run (2026-11-07)

**Result: the 71a-2 rule fails on criterion 1, so `NLU_SCHEMA` stays `flat`.**
Acts misses one scripted turn out of 92: a turn from a held-out script. Flat misses that turn and 14 more. Acts
passes criteria 2, 4 and 5. Criterion 3 (amounts) failed on the run made under the rule (am14).
A later re-run after a converter fix passed, but that re-run came after seeing the result
(see below).

## Setup

- Model `anthropic/claude-haiku-5-5`, effort low, one target, no fallback:
  `docs/eval/nlu_haiku_20261008/providers_haiku.yaml`, profile `claude_haiku_nlu`.
- `--no-cache` (every call live). Ref date 2026-04-01 (corpus), scripts as written.
- Corpus records: `docs/eval/records/nlu_corpus_{flat,acts}_p71a2_{main,amounts,hstance,hneg}.jsonl`
  (+ `acts_p71a2_amounts_fix`); sections `flat_P71A2_*` / `acts_P71A2_*` in `docs/eval/nlu_corpus.md`.
  Scripted records: `scripted_flat.json`, `scripted_acts.json` here.
- Commands: `python -m eval.nlu_corpus --label <s>_P71A2_<file> --corpus <F> --schema <s> --no-cache --profile claude_haiku_nlu --providers <file>`;
  `python -m eval.scripted_calls.run --nlu <s> --profile claude_haiku_nlu --providers <file> --out docs/eval/nlu_v2_20261107_r2`.

## Which numbers are held out

- **Held out (frozen in commit 2155174 before any 71a-2 change, run once per schema):**
  `tests/nlu_corpus_heldout_negotiation.jsonl` (42 lines) and the 7 `neg_*` scripts plus the
  updated `operator_rule_correction` (seeded rules).
- **Not held out:** the main corpus (71a tuned the acts prompt on it), the amounts file
  (71a's misses there were the 71a-2 fix list) and the held-out stance file (seen in 71a's
  final run; not tuned on). The older scripts were written in 71a.
- 71a-2 changed the acts prompt and converter after writing the frozen set. No frozen line or
  script was run until all changes were in place. The one exception is the am14 converter fix:
  it went in during the held-out negotiation runs, before the acts run of that file started.
  It affects only a percent quote that also covers a "$X total". No held-out line has that
  shape (hn18–hn21 are totals with no percent), so their results are the same either way.

## The rule (docs/NLU_PLAN.md, "Rule for 71a-2"), criterion by criterion

| # | Criterion | Flat | Acts | Bar | Pass? |
|---|---|---|---|---|---|
| 1 | Scripted calls: every expected move holds (19 scripts, 92 turns) | 77/92, 14/19 | **91/92, 18/19** | all | **FAIL** (`neg_rule_change` #2) |
| 2 | Held-out negotiation (42): stance | 0.905 | 0.929 | ≥ 0.885 | PASS |
| 2 | Held-out negotiation: terms (n=31) | 0.968 | 0.968 | ≥ 0.948 | PASS |
| 3 | Amounts (14): terms (n=13) | 1.000 | 0.923 (am14) | ≥ 0.980 | **FAIL** (run under the rule); 1.000 on the re-run after the fix |
| 4 | Main (193): stance | 0.922 | 0.907 | ≥ 0.902 | PASS |
| 4 | Main: terms (n=76) | 0.908 | 0.921 | ≥ 0.888 | PASS |
| 4 | Held-out stance (32): stance | 0.969 | 0.969 | ≥ 0.949 | PASS |
| 4 | Held-out stance: terms (n=16) | 0.875 | 1.000 | ≥ 0.855 | PASS |
| 4 | Filler false accepts (all files) | 0 | 0 | 0 | PASS |
| 5 | NLU p50 ms (main / amounts / h-stance / h-neg) | 1251 / 1316 / 1258 / 1220 | 1006 / 1042 / 998 / 1024 | ≤ flat | PASS |
| 5 | Offline oracle eval | — | PASS, `eval_20261107_164627_s7`, turns_to_proposal 6.714, turns_to_outcome 5.390 (unchanged) | PASS | PASS |

Private-info recall (reported, not a blocker since D21): flat 40/40, acts 39/40 (main f25
"right right, and the client pays how much each month?"; hs27 now caught). Output tokens per
line: flat about 205, acts about 50–90.

### Criterion 1: the failing turn

`neg_rule_change` #2, held out. After our 31% counter, the rep says "Actually, I just checked, on
this account it's six payments max, not eight." Both schemas read it as a plain rule (stance `info`):
the code's revision cue (`_REVISION_RE`) does not match "actually … not eight". So the orchestrator's
post-proposal revision path does not run. The new value contradicts the known 8, and the agent asks
"Earlier you mentioned 8, and now I am hearing 6. Which of those should I use?" The script expected
the agent to take 6 and hold its price. Not fixed here: a fix made after seeing a held-out failure
would no longer be held out. It is left for the next phase, with a fresh script.

The first acts scripted run crashed partway (`TypeError`: the new total reader hashed a tier
list value). That was a code bug, not a tuning change; it was fixed and the scripts were run
once more. The table shows that run.

### Criterion 3: am14

"Fifty percent, which comes to $900 total." The model returned only the percent, and its quote
covered the whole line, so the new code total reader counted the $900 as already claimed.
Fix: money inside a percent-only price quote is no longer counted as claimed (unit test added).
Re-running the amounts file on acts gave terms 13/13 and stance 12/14 (am11 info vs label counter;
am13 read as a question this time). Flat's amounts stance is 0.786 (am09–am11 `info` vs `counter`). The
fix does not touch any scripted line, so the scripted run was not repeated.

## Per-file misses (for triage)

- Held-out negotiation, both schemas: hn03 "Fifty's too low. Meet me at fifty eight." → `reject`
  (code reject cue "too low" wins over the counter; label `counter`); hn35 / hn36 read-back
  corrections → flat `counter`, acts `other` (label `info`, terms right in both); hn32 "Twelve
  payments max, and they have to be even." loses `even`. Flat also: hn15 hedged ask → `stall`.
  **Possible label issues (not changed; the set is frozen):** hn35 / hn36 stance (`info` follows
  the read-back fast path; the model path gives other labels, and the policy reads the term, not
  the stance) and hn03 (`reject` + counter price is a defensible reading).
- Main acts stance misses: n08 i03 i04 f01 f14 f16 f17 f23 f27 a10 a13 a16 x06 x07 x08 e09 k02 k03.

## Cost

All 71a-2 paid calls on Haiku: corpus about 1.10M input / 0.075M output tokens, scripted about
0.36M / 0.029M, plus the crashed partial acts scripted run (under 0.17M / 0.01M). In total about
1.6M input and 0.11M output tokens, **about $0.22** at $0.10 / $0.50 per Mtok (cap $2.00).

## Decision

`NLU_SCHEMA` stays `flat` (`app/config.py`). Acts is ahead on the scripted calls (91 vs 77 of 92)
and on latency, and level or better on every corpus file except amounts stance (that file's
labels follow the settled rule now). What blocks it is one held-out turn that flat also fails.
Suggested next step: make "actually … not <old value>" a revision cue (it helps both schemas),
then re-check it on a newly written held-out script.
