# Design decisions

The agent negotiates a debt settlement over voice or text. Each turn, a
language model reads the representative's line into structured terms (NLU);
code checks that reading against the words actually said; a deterministic
policy picks the next move; the feasibility engine ([`ENGINE.md`](ENGINE.md))
computes every number; and the model phrases the move as a template with placeholders that
code fills from public facts (NLG). Two guards check every sentence before it
is spoken, and everything is written to an append-only audit log. The records
below explain why it is built this way. Each ADR is context, decision,
consequences; numbers link to the committed evidence in [`docs/eval/`](eval/).

| ADR | Decision |
|---|---|
| [1](#adr-1-code-owns-the-policy-no-react-agent-in-the-product) | Code owns the policy; no ReAct agent in the product |
| [2](#adr-2-the-llm-writes-placeholders-never-digits) | The LLM writes placeholders, never digits |
| [3](#adr-3-public--private-facts-and-role-scoped-streams) | PUBLIC / PRIVATE facts and role-scoped streams |
| [4](#adr-4-effects-commit-on-the-speech-ack) | Effects commit on the speech ack |
| [5](#adr-5-feasibility-is-non-monotonic-so-scan-the-curve) | Feasibility is non-monotonic, so scan the curve |
| [6](#adr-6-claude-reads-the-rep-on-the-demo-under-a-daily-cap) | Claude reads the rep on the demo, under a daily cap |
| [7](#adr-7-how-negotiation-decisions-are-made) | How negotiation decisions are made |
| [8](#adr-8-engine-v2-exact-search-objectives-explanations) | Engine v2: exact search, objectives, explanations |
| [9](#adr-9-voice-on-pipecat--deepgram-over-webrtc) | Voice on Pipecat + Deepgram over WebRTC |
| [Voice turn](#one-voice-turn-annotated) | One voice turn, annotated with measured stage timings |

## ADR 1. Code owns the policy; no ReAct agent in the product

**Context.** A tool-calling (ReAct) agent is the obvious way to build this: give
an LLM `counter`, `confirm`, `escalate` tools and let it reason. But every move
here moves money, and the model would need the client's private ceiling to
choose a counter.

**Decision.** `decide()` in `app/agent/policy/cascade.py` is plain Python: belief state,
verified NLU and the affordability curve in, one `Action` out. The LLM only
reads (NLU) and phrases (NLG). The ReAct and LLM-only agents exist, but only as
eval arms in `eval/agents/` (Phase 24a); nothing in `app/` imports them.

**Consequences.** Policy is unit-testable without a model, and the offline eval
gates it on every push ([policy eval](eval/policy_eval_20261009/summary.md):
valid agreements 1.0, n=23). The eval arms must be given private figures to
work at all, which is the risk this decision avoids. The cost is rigidity: a
new move needs code and tests, not a prompt edit.

**A/B result.** The decision was tested against the alternatives on the same 48
seeds, with live NLU and live creditor phrasing
([summary](eval/ab_20261007/summary.md), [decision](eval/ab_20261007/decision.md)).
The adoption rule was written down before the run and was not moved afterwards.
The policy with template NLG (A) stays the default. The policy with
conversational acts (B, H3) was judged more natural, winning 0.80 [0.61, 0.91]
of decisive pairs against A, but it fails the rule: it spoke 1 private figure,
from a policy accept of a figure the simulated creditor invented, and its
`agreement_valid` was 0.71 against A's 0.75. Both of those agreement failures
come from live-NLU extraction errors that every arm shares. The ReAct agent (C)
fails on safety (2 private and 8 unverified figures spoken in its own text),
escalation (0.22 against 1.00) and latency (10.2 s p50 against 2.0 s). The
LLM-only arm (D) was stopped at 13 of 48 scenarios and was behind A on every
check it reached. A blind human check of 20 B-vs-A pairs preferred B in 11 of
15 decisive pairs, against the judge's 0.80
([ratings](eval/ab_20261007/summary.md#human-check-of-the-judge-added-in-phase-50)).

## ADR 2. The LLM writes placeholders, never digits

**Context.** LLMs paraphrase numbers: "about sixty", "$1.2k", a rounded total.
On a settlement call a wrong figure is a misquote the firm may be held to.

**Decision.** NLG writes a template such as "We can offer {offer_pct}, that is
{offer_total} over {num_payments} payments." `template_guard` rejects any digit,
`$`, `%`, number word or unknown placeholder. Code fills placeholders from
`Fact.render()`. `rendered_guard` then rejects any figure that is neither a
PUBLIC fact nor a number the creditor said, plus commitment phrasing. A blocked
line falls back to a fixed safe sentence.

**Consequences.** Every spoken number traces to an engine or creditor fact; the
policy eval reports 0 unverified figures spoken. Templates can be built offline
and reused: the demo's `NLG_MODE=bank` serves guard-checked templates with no
LLM call, which took NLG off the reply path ([latency](eval/latency_20261007.md):
`nlg_ms` p50 1440 → 0). The price is stiffer wording, and number words like
"first" must be avoided in copy. The short acknowledgement the agent speaks
before its move ("Understood, up to 5 payments.", Phase 46b) needs no LLM at
all: code picks the wording from a few fixed variants and fills only terms the
creditor said, never a private figure, through the same two guards. If the rep
then says we misheard ("I said six", "you misheard"), the new value replaces the
acked one, and so does a change request ("can you change it to $250", "make
that 6", Phase 54) that changes that one term. A bare "No, it's six" may be the
rep restating their own rule, so the agent asks which value to use (Phase 49).
A short reply that names one side ("$250", "the new one") then settles the
question in code, without a model call or the total-or-per-payment question.
Since Phase 50 the payment structure is acked too ("Got it, a balloon
schedule."), through a `text` fact: a fixed, digit-free phrase chosen by code
from the structure the rep stated, so it adds no figure to check.

## ADR 3. PUBLIC / PRIVATE facts and role-scoped streams

**Context.** The agent needs the client's balances, fees and maximum affordable
percentage to negotiate. None of it may reach the creditor, in speech or on the
wire.

**Decision.** Every `Fact` carries `visibility`. The NLG prompt receives PUBLIC
facts only. Private values form a blocklist that `rendered_guard` checks. On the
socket, `app/voice/views.py` filters every frame: `?view=rep` drops the
affordability curve, `max_bp`, fees, balances, rescue amounts and any audit row
not on an allow-list; `?view=operator` gets everything.

**Consequences.** Privacy is testable: whole calls run on the rep view and every
frame is scanned for every private value (`tests/unit/test_ws_views.py`). A new
audit event is operator-only until someone allow-lists it. The operator view is
public on the hosted demo by design, because all data is synthetic; the rep view
is the privacy-scoped stream. A real deployment would put the operator view
behind auth.

## ADR 4. Effects commit on the speech ack

**Context.** In voice, the agent may say "we can do forty percent" and be
interrupted before the creditor hears it. If state updated when the line was
generated, the agent would believe it had offered something nobody heard.

**Decision.** `Action.effects` are deferred. Each sentence is acked once the
rep has heard it: in voice, when the server has played its audio (ADR 9); only then does `apply_effects` commit agreed
terms, phases and drafts. On barge-in, effects of unheard sentences are dropped;
if the rep heard part of a question (ASK / CLARIFY) before talking over it, its
question counts still apply (Phase 54), so a question the rep keeps talking
over still reaches its handoff.
A small set of bookkeeping effects (`offer_counter`, `record_confirm`,
`set_phase`, …) applies on emit, so a fast "yes" during TTS wraps instead of
re-confirming. A barge that heard none of the reply undoes the whole turn,
eager effects included (Phase 56): the state from before it is restored, the
voice session is told to drop those sentences (`speech_dropped`), and the rep's line
is joined to their next line, which goes through NLU once. This is the usual
case when a pause splits one rep sentence into two speech clips. A late
`sentence_done` for a dropped sentence is ignored.

**Consequences.** Belief and the audit log match what was actually said. Text
mode uses the same path: with voice off the browser acks what the rep has read
(the opening on the rep's first typed line, then each line as it arrives;
D17), and autoplay auto-acks on the server. The ordering is subtle, so it is pinned
by orchestrator and WebSocket tests (barge-in during NLU, merged turns).

## ADR 5. Feasibility is non-monotonic, so scan the curve

**Context.** It is tempting to binary-search the highest affordable
percentage. But a lower offer can fail where a higher one passes, because each
payment must clear the creditor's minimum: a pattern like `0000111100001111`.

**Decision.** The adapter evaluates the whole grid, `1%…100%` in 100 steps,
and caches it (`affordability()`, `lru_cache` on scenario, rules, start date and
engine mode). Policy reads `max_bp` and the feasible set; counters snap onto
feasible grid points. Since Phase 59 each point is `engine.is_feasible`, which stops
at the first feasible plan: whether a price fits does not depend on how plans are
ranked or how the fee is timed ([`ENGINE.md`](ENGINE.md#4-objectives-and-fee-policy)).

**Consequences.** Correct by construction for any curve shape. The scan costs a
median 0.46 ms on the demo fixture (8.3 ms when first measured with the take-home engine) and at
most 4.8 ms on 600 random full-size cases
([benchmark](eval/engine_v2_20261028/bench.md)); a cache hit is under 1 ms. The
policy eval's 0 stuck calls and the invariant sweep (counters never above the
private max) rely on it. The grid is 1-point granular; finer offers would need a
denser scan.

**What engine v2 found about the curve (Phases 57–61).** The take-home engine
searched only part of the legal plans, so a hole in the curve could have been an
artefact. The full-size hunt found such false holes only on rules built to expose
them (2 of 20,000 adversarial cases called infeasible with a legal plan available,
none on the scenarios or 20,000 random cases), and the exact search removed them
([gap report](eval/engine_gap_20261025_full/notes.md)). With the exact engine the
holes that remain are real: 57 of 537 random full-size curves with a feasible point
are still non-monotone, and every scenario curve fails at its lowest prices on the
payment floors ([`ENGINE.md`](ENGINE.md#8-the-affordability-curve)). So the full scan
stays, and now each point is also proven.

## ADR 6. Claude reads the rep on the demo, under a daily cap

**Context.** The NLU step decides what the representative meant, and the
free-tier models were weakest there. On the same 183 corpus lines and the same
prompt, Claude Sonnet 5.5 scored stance accuracy 0.820, private-info recall
1.000 and term exact-match 0.984
([`AFTER_P39_CLAUDE`](eval/nlu_corpus.md#after_p39_claude)), and it got all 14
amount lines right where Groq got 0.857 of them
([`AMOUNTS_P39`](eval/nlu_corpus.md#amounts_p39)). Phase 41 put Sonnet on the
demo. Claude Haiku 5.5 costs about 1/19 as much, but on that prompt it scored
stance accuracy 0.601 and private-info recall 0.941: it heard rule statements
as offers ([Phase 40 summary](eval/nlu_haiku_20261008/summary.md)). Phase 42
rewrote the prompt's stance and private-info guidance for Haiku (0.896 on the
corpus, 0.906 on a new held-out set;
[Phase 42 summary](eval/nlu_prompt_p42/summary.md)). Phase 43 added one line
(stance never changes which terms are extracted) and confirmed it. Claude is
paid, and the hosted demo is public.

**Decision.** Haiku 5.5 reads the rep first. The `demo` profile's NLU route
is Haiku 5.5 (effort `low`, the 6 s NLU timeout), then the old free chain
unchanged. The Haiku target is `budgeted`: spend is summed per UTC day from
each live reply's token counts at the prices in `config/providers.yaml`
($0.10 / $0.50 per million tokens), as integer micro-dollars in the app DB.
Once the day reaches `CLAUDE_DAILY_BUDGET_USD` (default $1.00), the target is
skipped and the call goes to Groq until 00:00 UTC. Each call first reserves
an upper-bound estimate atomically and settles it to the real usage after, so
concurrent calls overshoot the cap by at most one estimate; a timed-out call
is charged its prompt estimate, since it may still be billed (Phase 47). A
budget never blocks a call. Phrasing (NLG), speech-to-text and "Watch a call" are unchanged. Sonnet
5.5 remains the eval `judge`, which is never budgeted. Without
`ANTHROPIC_API_KEY` the route behaves exactly as before.

Why Haiku over Sonnet, on the final prompt (Phase 43 runs, one each):

| | Haiku 5.5, P43 prompt | Sonnet 5.5, P39 prompt (Phase 41 demo) |
|---|---|---|
| stance accuracy, corpus (183) | **0.869** ([`HAIKU_P43_FIX`](eval/nlu_corpus.md#haiku_p43_fix)) | 0.820 |
| stance accuracy, held-out (32) | 0.969 | not run |
| private-info recall, corpus / held-out | 1.000 / 1.000 | 1.000 / not run |
| term exact-match, 66 lines with terms | 62 | 63 |
| amounts (14 lines) terms correct | 14 / 14 | 14 / 14 |
| filler false accepts (31) | 0 | 1 |
| NLU request p50 / p95, shipped route (n=20) | **1.17 s / 1.81 s** | 2.07 s / 2.70 s |
| cost per NLU call | **$0.00029** | $0.0046 |

Sonnet was not rerun on the new prompt (cost cap, Phase 42).

**Consequences.** $1 buys about 3,400 rep turns a day on Haiku, against about
215 on Sonnet ([live probe](eval/haiku_nlu_demo_20261008/summary.md)), and the
line is read about 0.9 s faster at p50, slightly faster than Groq. Haiku still
over-flags commitment demands (5 false positives on the corpus vs 2 for Sonnet)
and drops a few terms, which makes the agent ask again; `post_verify` never
lets a wrong number through. After the cap the demo reads the rep with the free
models, so quality can change mid-call; Groq has not been measured on the new
prompt. The audit shows which model answered each turn (`llm` rows, operator
view). The rule-based stance guards still run on Claude's output, and Phase 37
showed they override some correct Claude labels. Since Phase 45 an accept,
reject or firm phrase inside a question, after a conditional ("if", "whether")
or after a negator no longer forces a label (Haiku probes 2/12 → 10/12, corpus
unchanged; [ADR 7](#adr-7-how-negotiation-decisions-are-made)). Render's disk is ephemeral, so a restart starts the day's count at
zero; a monthly limit in the Anthropic console is the backstop.

A few reading rules run in code after any model, so a fail-over to a weaker
model loses less (Phase 49, [rescore](eval/nlu_rules_20261009/summary.md)).
Questions about money held for the client (escrow, program or settlement
account), "the most they could pay" and SSN digits count as private asks.
Clear insults and threats ("shut up", "waste of my time", "report your
firm", "are you stupid") reach the hostility threshold. The model's "total or
per payment?" flag is dropped when the sentence only states the balance. A
bare demanded amount ("We would need $600 from the client.") always gets that
question. Apart from the balance-statement case, the rules only add a flag
or a question. When no rule fires, the model decides. On the saved runs they lifted hostility recall to 1.0 for Haiku, Groq
and Cerebras, and Cerebras private-info recall from 0.882 to 1.0. No line got
worse.

Phase 54 measured giving the NLU prompt the last 3 spoken turns as context,
and did not ship it. On a new 16-line context set, terms went from 14/16 to
13/16, bare amounts stayed ambiguous, and each turn cost about 79 more tokens
([measurement](eval/nlu_context_20261013/summary.md)). The model still reads
one line plus the agent's last sentence. The call that prompted the test is
fixed in code instead.

## ADR 7. How negotiation decisions are made

**Context.** Up to Phase 44 the policy accepted any affordable ask: the rep's
first number could be confirmed without a counter (`gap_small`,
`no_lower_counter`), and an "accept" stance carrying a new percentage was taken
as agreement. In the A/B run (pair 17, `s0007_006`) the agent answered "Great,
100% is acceptable" to a line that only mentioned "the 100% balance". Calls
could also end with a plain "no deal", leaving nobody to follow up, and a rep
who kept repeating themselves could keep the agent asking the same question.

**Decision.** The rules live in `_negotiate` in `app/agent/policy/ladder.py`; the
engine is unchanged. The tunable settings (in `.env`):

| Setting | Default | What it does |
|---|---|---|
| `ACCEPT_LINE_PCT_OF_MAX_BP` | `7500` | The accept line: 75.00% of the client's maximum, rounded down. The agent never offers or accepts more. |
| `ANCHOR_RATIO` | `0.7` | The first counteroffer is about 70% of the lower of the ask and the accept line. |
| `CONCESSION_FACTOR` | `0.5` | When the representative comes down, the agent moves up by half as much. |
| `MAX_COUNTERS` | `6` | At most six counteroffers in a call, including "we are staying at" repeats. |
| `MAX_SAME_QUESTION` | `2` | The agent hands off instead of asking the same question a third time. |
| `MAX_NO_PROGRESS_TURNS` | `4` | The agent hands off after four replies in a row that add nothing new. |
| `MAX_TURNS` | `24` | The hard limit on call length, also a hand-off. |
| `ENGINE_OBJECTIVE` | `fee_first` | How the engine ranks legal plans: `fee_first` (the program fee as early as possible, then fewer payments) or `client_first` (most money left in the client's account, then the smallest largest payment). See [ADR 8](#adr-8-engine-v2-exact-search-objectives-explanations). |
| `ENGINE_FEE_POLICY` | `front_loaded` | When the program fee may be taken: `front_loaded` (as early as the balance allows) or `proportional` (never ahead of the share of the settlement already paid). |
| `min_cushion_cents` (firm, `firm.json`) | `0` | The firm's minimum client cushion, $0 to $5,000: the plan keeps every balance at or above it when some plan at that price can. The operator can change it for one call in the Debt negotiator view; it never changes which prices are affordable. PRIVATE. |

On every turn, `decide()` checks the same list in a fixed order: the turn
limit, hostility, requests for private information, demands for a
commitment, replies that add nothing, contradictions, tentative values to
read back, missing rules, and only then price. The price rules:

- *Accept line.* We accept only at or below 75% of the client's ceiling
  (`accept_line_pct_of_max_bp = 7500`, rounded down to a whole basis point), and
  we never offer more than that. Any accept still needs a schedule the engine
  can build.
- *First number.* We always counter the rep's first number, at 70% of the lower
  of their ask and the line. Only if no lower percentage can be scheduled may we
  accept it straight away. A "yes" that names a percentage we never offered is
  their ask, not an agreement.
- *They come down.* We move up by half as much as they moved, and the pause
  count below starts over.
- *They do not move.* We hold once (repeat our offer and ask them to come down),
  then take two equal steps that split the gap between our offer and the lower
  of their number and the line; the second step lands on it. After that we
  accept if their number is at or below the line; otherwise a person takes over.
  (Phase 50; Phase 45 took two quarter-gap steps, which handed calls off well
  short of the line, e.g. 46 → 51 → 54% against a 66% line.)
- *They say it is final.* Above the line: a person takes over at once. At or
  below the line: one last offer halfway between our last offer and their
  number, and if they repeat their number we accept it.
- *Cap.* After `max_counters` (6; 4 before Phase 46a) spoken counteroffers, holds included, we
  accept if their number is at or below the line, else hand off.
- *Endings.* A call ends only as a confirmed deal or a handoff. Every former
  no-deal ending is now a handoff with a short spoken reason and a reason code
  for the person taking over (`infeasible`, `no_legal_counter`, `max_counters`,
  `above_accept_line`, `confirm_unacked`, `confirm_rejected`, `max_turns`,
  `wants_to_end`, `rep_ended`). When the rep wants to end, the agent says a
  specialist from our side will follow up.
- *Loop guard.* The agent hands off instead of asking the same question a third
  time (`repeated_question`), or after four rep turns in a row that add nothing
  new (`no_progress`). The 24-turn cap stays as a backstop.

**Consequences.** On the offline eval (seed 7, 100 calls) the deal calls are
unchanged (23 deals, surplus 0.689) and the 22 no-fix calls now hand off instead
of ending; no-fix calls are shorter (138 rep turns against 189) and speak fewer
counters (44 against 95 over the run)
([policy eval](eval/policy_eval_20261009/summary.md)). Since Phase 46a a
quarter of the simulated reps haggle (hold their number, concede in varied
steps, go firm at a floor between our first counter and the line, or stall),
so the eval exercises every ladder branch: hold, step, half-move
concession, final counter, accept on repeat, the cap (now 6) and both loop
guards ([46a eval](eval/policy_eval_20261009_46a/summary.md)). The Phase 50
equal steps changed 4 of the 100 calls: two holders now settle one point higher
in three fewer turns (surplus 0.883 → 0.873), and no outcome changed
([Phase 50 eval](eval/policy_eval_20261009_50/summary.md)). Deals above 75% of the ceiling, which the old policy could
reach, are now handed to a person. The scenario labels follow the same line:
a deal is "possible" only when the rep's floor is at or below it.

## ADR 8. Engine v2: exact search, objectives, explanations

**Context.** The engine came from a take-home exercise and was vendored
read-only. It had three weaknesses (decision D1 in the
[decision record](history/DECISIONS.md)). Its candidate family was structured,
not exhaustive, so it could call a price infeasible when a legal plan existed and
the agent would walk away from a deal it could make. Its ranking served only the
firm's fee timing. And an infeasible price came back as a bare `false`, so the
agent's term suggestions brute-forced up to 24 × 100 engine runs and the trace
could not say why a price did not fit.

**Decision.** Own the engine (`feasibility/`, linted and tested like the rest of
the repo) and measure before changing it: a brute-force oracle over every legal
plan on small cases, an exact full-size reference, a frozen golden corpus of the
take-home's outputs, and a gap report. Then, in order:

- *Exact search.* One ledger simulation per client turns every money rule into a
  cap on the plan's running total (the prefix-cap identity). Feasibility, fee
  timing and both rescue amounts only improve when running totals are lower, so a
  small family of plans that dominates every legal plan, searched by branch and
  bound, is exact. The take-home family was kept as the first stage, so outputs
  change only where it was not exact.
- *Objectives.* `fee_first` (the take-home ranking, the default) and
  `client_first`, plus an optional `proportional` fee policy and a per-firm soft
  client cushion. Keys written down before code; client-first measured, then not
  made the default because more payments cost clients more in bank fees (D8).
- *Explanations.* `explain` gives the reason, date and exact shortfall for an
  infeasible price; `what_if` answers term changes on the shared `Problem`, and the
  agent's term suggestions use it.

The full write-up, with the dominance argument and the exactness table, is
[`ENGINE.md`](ENGINE.md).

**Consequences.** The take-home family was exact on every scenario and on 20,000
random full-size cases, and missed only on tiered staircases with three or more
payment amounts and tight cash: 25 misses in 20,000 adversarial cases, up to a
$161.78 lump asked when none was needed
([gap report](eval/engine_gap_20261025_full/notes.md)). The engine now has 0
misses against the reference and the oracle, every schedule passes the
independent validator, and the golden corpus is byte-identical. No agent call
changed: the 100-call policy eval is identical to before
([Phase 61 run](eval/engine_v2_20261028/summary.md)). Scans got faster, not slower
(0.46 ms on the demo; golden worst case 10.8 s → 78 ms ranked,
[benchmark](eval/engine_v2_20261028/bench.md)). The shortfall reaches the operator
trace only; it is PRIVATE and blocklisted from speech. Open: the proportional fee is
exact over the search's family only, client-first has a slow adversarial tail, and
completeness of the take-home family without tiers stays a conjecture.

## ADR 9. Voice on Pipecat + Deepgram over WebRTC

**Context.** Voice first ran in the browser: `speechSynthesis` spoke, vad-web cut
the mic into clips, and the server sent each clip to Groq Whisper (or the browser
ran `webkitSpeechRecognition`). Three rounds of fixes made it worse
([D12](history/DECISIONS.md)): Chrome does not use its own speech as an
echo-cancellation reference, so the agent heard itself, and every guard against
that echo also ate real speech; batch STT per clip and silence-only turn
detection added latency and lost words. Measured on the same injected rep audio
([baseline](eval/voice_baseline_20261028/legacy-server-inject/summary.md)): first
reply audio 4463 / 5172 ms p50 / p95, numbers heard 20 of 24, the first word lost on
4 of 30 lines, 4 turns split by a pause.

**Decision.** Pipecat inside the FastAPI app (`app/voice/pipeline/`), audio over
WebRTC both ways so the browser's echo cancellation has the agent's audio as its
reference, Deepgram Flux for streaming STT with model-based end of turn, and
Aura-2 for streaming TTS. A spike and a harness came first, with a go / no-go
([spike](eval/voice_spike_20261028/summary.md)). The orchestrator stays the only
decision maker: Flux's final transcript runs the same turn path as typed text,
and "heard" means the sentence's audio has been written to WebRTC, so ADR 4's
acks come from what was actually played. Barge-in is two steps
([D15](history/DECISIONS.md)): a server VAD pauses the agent when the rep starts
talking over it, and Flux's words decide (two non-backchannel words stop it and
drop the rest; a backchannel or quiet resumes). A number the code cannot confirm
in the transcript is read back, not dropped ([D14](history/DECISIONS.md)). The
pause was made less sensitive after the user's laptop test, and the new path
became the default ([D16](history/DECISIONS.md)). Phase 67 removed the browser
path, its server STT route and the `stt` model role
([D17](history/DECISIONS.md)). Deepgram keys stay on the server, and spend is
capped per UTC day (`VOICE_DAILY_BUDGET_USD`, audited as `stt_session` /
`tts_request`).

**Consequences.** On the same injected audio, after tuning
([Phase 65](eval/voice_tuned_20261029/summary.md)): every reply turn got a
reply (13 / 13), numbers heard 12 / 12, no lost first words, split turns or false
barges, real interruptions 2 / 2 stopped, first reply audio 3185 / 4829 ms p50 /
p95. That misses the 2000 / 3000 ms targets; about 1.95 s of it is the NLU
reading the line, which voice cannot remove. Speech now costs money (about
$0.008 per minute of voice session plus $0.03 per 1,000 spoken characters) and the venv
grew by about 500 MB. Not yet measured: recognition through real laptop speakers
and mic (the harness's `afplay` lines were mostly not heard;
[client run](eval/voice_client_20261029/summary.md)), other devices, and the
hosted demo, which needs a TURN server.

## One voice turn, annotated

Per-stage p50s are the Phase 65 tuned run ([`voice_tuned_20261029`](eval/voice_tuned_20261029/summary.md):
injected rep audio in Chrome, both scripted calls, free Groq NLU chain, template /
bank NLG). Times are from the end of the rep's clip.

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser (mic, agent audio)
    participant V as Voice pipeline (Pipecat, Flux, Aura-2)
    participant W as Call socket /ws/call
    participant O as Orchestrator
    participant L as LLM client (by role)
    participant G as Policy, engine, NLG bank + guards

    B->>V: mic over WebRTC
    Note over V: Flux end of turn p50 651 (eager guess 313)
    V->>W: final transcript (shown as the rep's line)
    W->>O: creditor turn
    O->>L: NLU
    L-->>O: TurnAnalysis
    O->>G: verify, belief, affordability, decide(), template, guards
    G-->>O: line (final → reply p50 1946, mostly NLU)
    O->>W: say (UI) and the sentence to TTS
    V->>V: Aura-2 first byte p50 373 after the reply
    V-->>B: agent audio; first reply audio p50 3185
    V->>O: sentence played → ack, commit deferred effects
```

A short reply the code reads without the model ("Correct.") reaches first audio
in about 1.1 to 1.3 s. When the eager end-of-turn guess is right, NLU starts before
Flux commits the turn; when the rep keeps talking, the new words are merged into
the running NLU or the early turn is withdrawn. The earlier browser-speech path
(Whisper per clip, browser TTS) measured about 4.7 s p50 end of speech to first
audio as an estimate from stages ([latency_20261007](eval/latency_20261007.md));
its injected-audio baseline is in ADR 9.
