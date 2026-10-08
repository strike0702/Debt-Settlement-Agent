# NLU stance guards on Claude Sonnet 5.5 (Phase 37, 2026-10-08)

Measurement only. No change to `app/`, policy, NLU rules, prompts, reason codes,
thresholds or shipped profiles.

**Question.** With Claude Sonnet 5.5 doing NLU, how does the corpus score with
the deterministic stance guards (`repair_stance`) and without them (the raw LLM
stance)?

**Short answer.** On the 183-line corpus, Claude does almost as well without the
guards as with them. The guard changes **1 line of 183** (f19 `ok`), and that one
change helps. On 10 extra probe lines aimed at the known guard bugs (items 31.1
and 31.2), the guard **overrides a correct Claude label on 8 lines** and helps on
none. The corpus has no lines of that shape, which is why it never showed the harm.

## How it was measured

- Model: `anthropic/claude-sonnet-5-5`, effort `low`, one target with no fallback
  (`providers_claude.yaml` in this folder, profile `claude_nlu`, NLU timeout 60 s).
- One paid pass. Each record keeps two stances from the same reply:
  `predicted.stance_raw`, the reply parsed before any repair, and
  `predicted.stance`, what the agent uses after `repair_stance`. It also keeps
  `stance_rule`, the guard rule that decided the line. The runner checks that
  `repair_stance(stance_raw, …)` gives back the repaired stance for every LLM
  line. The guard-off row is a rescore of the same JSONL, with no extra calls.
- Corpus: `tests/nlu_corpus.jsonl`, 183/183 answered. 179 lines went to Claude.
  4 went through the bare-number fast path, which makes no LLM call and does no
  stance repair.
- Probes: `probes.jsonl` in this folder, 12 synthetic lines. 6 cover 31.1
  (questions and conditionals that contain an accept phrase), 4 cover 31.2
  (negated or contrastive reject phrases), and 2 are controls. They were
  hand-labelled before the run and kept out of `tests/nlu_corpus.jsonl`.
  q07 ("That's not too low.") is tagged `ambiguous`: its correct label is unclear,
  but `reject` is clearly wrong.
- Commands:
  - `python -m eval.nlu_corpus --label CLAUDE_P37 --profile claude_nlu --providers docs/eval/nlu_guard_20261008/providers_claude.yaml --concurrency 2 --audit-db eval/results/nlu_corpus_audit_p37.db`
  - `python -m eval.nlu_corpus --label CLAUDE_P37_NO_GUARD --from-records docs/eval/nlu_corpus_claude_p37.jsonl --no-repair-stance --profile claude_nlu --providers …`
  - the same as the first with `--corpus docs/eval/nlu_guard_20261008/probes.jsonl --label CLAUDE_P37_PROBES`
  - `python -m eval.nlu_guard_report docs/eval/nlu_corpus_claude_p37.jsonl --context docs/eval/nlu_corpus_before_p32.jsonl` for the tables below

## Cost (from the audit rows, at $2/M input and $10/M output)

| run | live calls | input tokens | output tokens | cost |
|---|---|---|---|---|
| pilot (corpus lines 1–10) | 10 | 10,455 | 1,201 | $0.033 |
| full corpus (pilot lines replayed from cache) | 169 | 177,281 | 22,071 | $0.575 |
| probes | 12 | 12,776 | 1,449 | $0.040 |
| **total** | 191 | 200,512 | 24,721 | **$0.648** |

The pilot projected $0.60 for the full corpus, under the $0.75 stop rule, so the
full run went ahead. Output never exceeded 121 tokens per call, so effort `low`
spends almost nothing on thinking. Median latency was about 2.1 s per call.

## Corpus: stance with and without the guard (183 lines)

Stance accuracy (8 labels): **0.820 with the guard, 0.814 without.** Filler false
accepts: 0 both ways (f23 → `counter`, f30 → `info`, both correct from the raw
LLM). Flags and terms are the same in both rows, since the guard only touches
stance.

| stance | pos | P on | R on | F1 on | P off | R off | F1 off |
|---|---|---|---|---|---|---|---|
| offer | 0 | 0.000 | n/a | n/a | 0.000 | n/a | n/a |
| counter | 22 | 0.769 | 0.909 | 0.833 | 0.769 | 0.909 | 0.833 |
| accept | 11 | 1.000 | 1.000 | 1.000 | 1.000 | 0.909 | 0.952 |
| reject | 9 | 0.900 | 1.000 | 0.947 | 0.900 | 1.000 | 0.947 |
| stall | 3 | 0.429 | 1.000 | 0.600 | 0.429 | 1.000 | 0.600 |
| info | 59 | 0.907 | 0.661 | 0.765 | 0.907 | 0.661 | 0.765 |
| question | 44 | 1.000 | 0.864 | 0.927 | 1.000 | 0.864 | 0.927 |
| other | 35 | 0.811 | 0.857 | 0.833 | 0.789 | 0.857 | 0.822 |

Per rule ("decided" means this rule was the first one to match the line):

| rule | decided | changed | helped | hurt |
|---|---|---|---|---|
| injection (never accept) | 7 | 0 | 0 | 0 |
| reject phrase | 3 | 0 | 0 | 0 |
| accept phrase (un-negated) | 5 | 0 | 0 | 0 |
| short ack | 6 | 1 | 1 | 0 |

The one changed line:

| id | line | label | raw Claude | guarded | rule | verdict |
|---|---|---|---|---|---|---|
| f19 | ok | accept | other | accept | short ack | helped |

f19 replies to the default agent line ("What settlement terms can you accept on
this account?"). Claude's `other` is a defensible reading of a bare "ok" there,
so this one "help" depends on how the line is labelled.

The 8 lines where a phrase rule fired are all easy cases ("Agreed.", "That's too
low.", "No, that doesn't work for us."), and Claude already had every one right.
Claude also never labelled an injection line `accept`, so the injection rule
changed nothing.

## Probes: items 31.1 and 31.2 (12 lines)

| id | line | label | raw Claude | guarded | rule | verdict |
|---|---|---|---|---|---|---|
| q01 | Let me check if that works for the client. | stall | stall | **accept** | accept phrase | hurt |
| q02 | Is that agreed? | question | question | **accept** | accept phrase | hurt |
| q03 | I need to see whether that works with my manager first. | stall | stall | **accept** | accept phrase | hurt |
| q04 | Has the payment schedule been agreed on your end? | question | question | **accept** | accept phrase | hurt |
| q05 | Before anything is agreed, I have to run it by compliance. | stall | stall | **accept** | accept phrase | hurt |
| q06 | Let me see if that works on our end and call you back. | stall | stall | **accept** | accept phrase | hurt |
| q07 | That's not too low. *(ambiguous)* | accept | other | **reject** | reject phrase | neutral (both wrong; reject is worse) |
| q08 | Fifty percent is not too low, we can accept that. | accept | accept | **reject** | reject phrase | hurt |
| q09 | The problem isn't that it's too low, it's the start date. | info | reject | reject | reject phrase | no change (Claude is wrong too) |
| q10 | No, that schedule doesn't work? Sorry, misread it. It does, we accept. | accept | accept | **reject** | reject phrase | hurt |
| q11 | That's too low for us. (control) | reject | reject | reject | reject phrase | no change |
| q12 | Yes, that works for us. (control) | accept | accept | accept | accept phrase | no change |

Raw Claude got 10 of 12 right. With the guard on, 3 of 12 are right.

- **31.1 (questions and conditionals that force accept):** confirmed, 6 of 6.
  Claude labelled each line `stall` or `question`, and the accept-phrase rule
  overrode it. In CONFIRM an accept closes the deal, so "Is that agreed?" would
  close the call.
- **31.2 (negated reject phrases):** confirmed. "not too low" is forced to
  `reject` (q07, q08), and so is a line that corrects itself (q10). q08 is the
  worst case: the reject rule runs before the accept rule, so the explicit
  "we can accept that" never counts.

The probe set is small (10 targeted lines plus 2 controls) and I wrote it, so
it shows the failure modes exist. It does not measure how often they happen in
real calls.

## Context: the current free-tier model (Gemini, P32 `BEFORE_P32`)

The only recent same-model rows are Phase 32's on Gemini `gemini-3.1-flash-lite`
(eval profile). Those records do **not** contain the raw LLM stance, and the
Phase 32 response cache is gone (it lived in a removed worktree). So the
with/without split cannot be computed for Gemini without new free-tier calls,
which this phase was not allowed to make. What can be shown:

- On the corpus, the phrase rules force the label on the same 8 lines as for
  Claude (f18, a01–a06, a14). The forced label is right on all 8. Whether Gemini
  would have got them right on its own is unknown.
- Gemini `BEFORE_P32` with the guard on: accept P/R 1.000 / 1.000, filler false
  accepts 0, f23 → `offer` (wrong, but not a false accept), f30 → `info`.
- The 31.1 and 31.2 probes have not been run on Gemini or Groq. The guard is
  lexical and runs after the model, so it forces the same wrong labels on those
  lines whatever the model says.
- History (not re-measured): Phase 15's accept precision gain on Groq
  (0.306 → 1.000) came mostly from narrowing the guard itself. Ack words moved
  out of the force-accept regex, and the short-ack rule now needs acks to
  outnumber content words. Most of the 21 BEFORE filler false accepts were
  likely produced by the old, broader guard, not by the LLM. Phase 28 found
  Groq labelling f23 / f30 correctly on its own.

## Conclusion (plain language)

- **Is Claude alone strong enough on stance?** On this corpus, yes. Without the
  guard Claude loses one arguable line (f19), and it handles injections, fillers
  and plain accepts and rejects correctly on its own. On the guard-bug shapes it
  beats the guarded output by 7 lines out of 10.
- **Which rules still earn their place (on Claude)?**
  - *Injection → never accept:* it changed nothing here, but it can only ever
    turn an accept into a non-accept on instruction-shaped text. That makes it a
    safety backstop that costs nothing when the model behaves. It earns its
    place as defence in depth.
  - *Short ack:* the only rule that helped (f19), and it hurt nothing. It is
    low risk, because it only fires when the line has no terms.
  - *Accept phrase:* no help on the corpus, 6 of 6 hurt on the 31.1 probes.
    Its errors are false accepts, the costly direction, since an accept in
    CONFIRM wraps the call.
  - *Reject phrase:* no help on the corpus, 3 hurt and 1 neutral on the 31.2
    probes. Its errors push the agent to treat an acceptable offer as rejected.
- **Caveat:** production NLU is Groq `gpt-oss-120b` (demo) with Gemini behind
  it, not Claude. These results do not show that the guards can come out of the
  shipped profiles. They show that the phrase rules add nothing for a strong
  model and are wrong on question, conditional and negated forms for any model.

## Options for 31.1 / 31.2 (the user decides)

1. **Patch the guard.** Extend the negation guard to `_REJECT_STANCE_RE` (31.2).
   Make the accept-phrase rule skip a clause that is a question (`?` or an
   interrogative opener) or conditional (`if / whether / before / once / until`,
   `let me check / see`) (31.1). Evidence (worked by hand against the
   probes, not run): q01–q07 would fall through to the LLM (6 right, q07 stays
   `other`), and q08 would reach the accept rule and come out `accept`, which is
   right. q10 (self-correction) would still be forced to reject. Cost: more lexical rules, each needing tests, and a new phrasing
   can always slip past them. The guard stays useful for weaker models.
2. **Remove force-accept (and force-reject), keep injection and short-ack.**
   Evidence: on Claude the phrase rules helped 0 of 183 corpus lines and hurt 9
   of 10 targeted probes. Risk: no raw-stance measurement exists for Groq or
   Gemini. A model that misses a plain "Agreed." would leave the agent
   negotiating, which is the safe direction. Before deciding, the same split
   could be measured on Groq for free once quota allows, by rerunning
   `eval.nlu_corpus` with the shipped profile and comparing `stance_raw` to
   `stance`.
3. **Leave as is.** The known false accepts on questions and conditionals in
   CONFIRM stay. The corpus will keep showing no harm, because it has no such
   lines.

No threshold change is recommended or implied.
