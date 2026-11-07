# Decision record: engine v2 and the work around it (from 2026-10-22)

Why each choice was made, what the evidence was, and what is still open. One entry per
decision, newest last. Phase details live in `docs/history/PROGRESS.md`; the plan and its
revisions in `docs/ENGINE_PLAN.md`. Numbers here are copied from the reports cited.

---

## D1. Scope: which engine weaknesses to work on (2026-10-22)

**Question.** The engine was still the take-home code, vendored read-only. Its author had
listed its limits: (1) the candidate family is structured, not exhaustive; (2) ranking
serves only the firm (fee timing, then fewest payments); (3) "infeasible" comes with no
reason, so the agent brute-forces term alternatives (up to 24 × 100 engine runs).

**Options.** Exact search with a proof; client-first ranking; exact max % to the basis
point plus speed; why-infeasible and what-if.

**Chosen.** Exact search + proof, client-first ranking, why-infeasible + what-if. Not the
exact-max-% item (speed became a budget, not a goal).

**Why.** These three are the engine's own stated weaknesses and they change what the agent
does on a call (walk away from a legal deal; offer a plan that suits the firm only; offer a
term change without knowing why it helps).

## D2. Own the engine instead of wrapping it (2026-10-22)

**Options.** Keep `feasibility/` vendored and add new logic in the adapter, or make it an
owned package.

**Chosen.** Owned v2 (Phase 55). Same package name (11 importers unchanged), ruff-checked,
settlement % as integer basis points (`Offer.settlement_bp`, `CreditorRules.program_fee_bp`,
no float on any money path), the take-home `evaluate_offer` kept as a wrapper.

**Rule that replaced "read-only".** Every engine behaviour change ships with an oracle or
golden test and a PROGRESS note (CLAUDE.md).

## D3. Build the evidence before changing behaviour (Phase 55)

**What was built.**
- `Problem`: the client's ledger is simulated once per (client, rules, start date) with no
  creditor payments and no fee. A k-payment plan only subtracts its running totals and bank
  fees from that base ("prefix-cap identity", `feasibility/problem.py`, tested).
- Golden corpus: 5 fixtures, 7 scenarios × 100 bp, 600 seeded random cases; output frozen.
- Brute-force oracle (`tests/engine/oracle.py`): every plan legal under the assignment's
  rules, on small instances; checked against the independent validator.

**Result.** No behaviour change (golden byte-identical, eval identical). The affordability
scan got faster as a side effect: 16.1 → 6.6 ms median on the demo, same machine, because
the ledger is simulated once per scan instead of once per candidate.

## D4. The small-case gap report found nothing: hunt large cases first (2026-10-25)

**Evidence.** `docs/eval/engine_gap_20261023/`: on 85,000 small random cases plus a
random-cap sweep (k ≤ 8), the take-home family matched the oracle exactly: 0 false
infeasible, 0 later fee, 0 rescue over-estimates.

**Options.** (a) Build the exact search anyway, justified by client-first ranking;
(b) merge it into the ranking phase; (c) first find out whether misses exist at full size.

**Chosen.** (c), a dedicated hunt (Phase 57), before writing engine code.

**Why.** Do not fix a bug that has not been shown to exist; the result decides the scope.

## D5. The hunt: misses are real but narrow (Phase 57)

**How.** An exact reference solver at full size (`tests/engine/reference.py`, up to 24
payments, 4 levels). It searches a *dominating family*: every legal plan has a family member
whose running totals are never higher, and feasibility, fee timing and both rescue amounts
only improve when running totals drop. So the family is enough. For staircases each early
level only needs values in a small box above its floor (`[lo, lo + c_m/gcd(c_l, c_m))`);
the last two levels have a closed form. Checked against the oracle on 15,000 small cases
(0 disagreements) and against an unpruned run on 900 full-size cases.

**Findings** (`docs/eval/engine_gap_20261025_full/`):

| set | cases | false infeasible | worse fee timing | rescue over-ask |
|---|---|---|---|---|
| golden (fixtures, scenarios, 600 random) | 1,305 | 0 | 0 | 0 |
| random full size | 20,000 | 0 | 0 | 0 |
| adversarial | 20,000 | 2 | 2 | 21 |
| structural sweep (ledger built per miss) | — | 112 verified | | |

- Every miss: staircase, ≥ 3 levels, minimum-payment tiers, tight cash.
- Mechanism: the take-home pins early levels at their floors; when the leftover sum is in a
  residue class the last two runs cannot absorb (runs sharing a factor, e.g. 1, 2, 2), that
  composition is dropped, and tiers stop other compositions from covering it.
- Worst case: the engine said infeasible and asked for a $161.78 lump sum when a plan with
  no extra money existed. Another: $44.00 asked where $14.18 was enough.
- Proved exact: even plans, balloon plans, staircases with ≤ 2 levels.
- Not proved: completeness without tiers (0 misses in 12,483 tier-free draws; a conjecture).

## D6. Fix it in one phase, rescue included (Phase 57b, 2026-10-27)

**Options.** Port the fix with exact rescue in one phase; keep search and rescue separate;
skip the fix and document the gap.

**Chosen.** One phase (old Phase 58 merged in): the same dominating family gives exact
lump sum and monthly increase directly (they are monotone in running totals).

**Result** (`docs/eval/engine_v2_20261027/bench.md`):
- 0 misses against the reference on golden, 20,000 random, 20,000 adversarial and all 130
  known witnesses; golden byte-identical; agent eval identical call for call.
- Much faster, not slower: worst golden scan 10.75 s → 78 ms; adversarial p95 2.9 s → 63 ms;
  demo 3.6 → 1.9 ms. The old path simulated about 7,000 candidates per settlement point.
- Deviation accepted: the old code path for even/balloon/≤ 2 levels was replaced too (it was
  the slow part); same winner by proof and on 5,600 random cases.
- Open: 6 of 2,000 adversarial scans over 150 ms (max 220 ms); plans with 6–8 levels take
  1–4 s.

## D7. Two rankings, measured before choosing (Phase 59)

**Built.**
- `fee_first`: exactly the take-home ranking (fee as early as possible).
- `client_first`: largest cushion, then the smallest largest payment, then fee-first.
- Optional `proportional` fee policy: fee collected never runs ahead of the share of the
  debt paid, modelled on the FTC Telemarketing Sales Rule advance-fee provision
  (16 CFR 310.4(a)(5)(i)); described as a stricter per-debt model, not legal advice.

**Measurements that changed the design.**
- "Money left after the fee" as the cushion tied across all plans in 40% of golden cases,
  because a front-loaded fee fills each window to its minimum. Cushion was redefined as the
  lowest balance with the whole fee set aside.
- "Smallest largest payment" is not monotone in running totals, so it needed its own search
  and proof; 0 mismatches against the oracle on 4,449 feasible cases.
- Proportional fee timing is not monotone either: exact only over the family (short of the
  best in 3.3% of small cases) and slow (up to 50 s per scan). Opt-in only.

**Effect of client-first on the agent.** Same outcome on all 100 eval calls; deals averaged
8.3 payments instead of 3.0; median cushion $800 vs $237; but the client pays a median $19
(max $38) more in bank fees per deal.

## D8. Fee-first default, per-firm soft cushion set per call (2026-10-27)

**Question.** Ship client-first as the agent's default?

**Chosen.** Fee-first stays the default. Each firm sets a minimum client cushion
(`firm.json` `min_cushion_cents`), which the operator can change for a single call in the
Debt negotiator view.

**Hard or soft.** Soft: the engine first looks for a plan that keeps the cushion; if none
exists at that price it falls back to the best plan without it. The agent's highest
affordable offer does not move. (A hard rule would lower the agent's ceiling.)

**Per call, firm default.** Not stored; no settings store or accounts. The rep never sees
it and it never reaches the language model (client-protection policy is private).

**Why.** Client-first's cushion came with extra bank fees; whether that trade is right is a
firm's policy, so it becomes a firm setting rather than a hard-coded ranking.

## D9. Leftover cleanup and what manual review caught (Phase 56)

**Planned items.** Equal-steps wording in the trace, no "1 payments", scenario cards and
expected outcomes, `.env.example`, mic stops after a handoff, the term-change row shows the
term asked for (not the rep's price), phone card scroll, screenshots.

**Found only by using it in the browser.**
1. A review server left running in offline mode (fake language model) made calls go silent.
   Underneath was a real gap: the console stored server errors but never showed them. Fixed:
   a plain error line in both views, raw message in the operator view only.
2. Voice: phantom rep lines ("and payment, payment, payment…") and the agent cutting
   itself off. Cause: Chrome's echo cancellation does not cover the browser's own speech, so
   the agent's voice reached the mic; Whisper, primed with domain words, turned echo/noise
   into those words. Fixed on both sides: drop low-confidence or hint-word-only
   transcripts on the server; a 0.7 s echo tail and stricter interruption rules while the
   agent talks. The thresholds are reasoned, not yet tuned on real laptop audio.

## D10. Late start date: working as designed, left as is (2026-10-27)

The console shifts scenario dates near today; the client's last savings draft lands on
May 15, 2027, and nothing can be scheduled after it, so a June 30 start cannot work. The
agent proposed April 30, the latest month-end before the last draft. Known weaknesses, not
fixed by decision: the agent does not tell the rep why the date fails, and it only tries
month-end dates (skips May 15).

## D11. Cushion field shows its state with a border (2026-10-28)

Review of the operator field went through three rounds: help text under the field →
tooltip (hover and keyboard focus); an "accepted" text label → removed as too much text;
final: no focus ring, border green when the value is accepted (Enter or blur), grey while
editing, red when invalid, with a visually hidden status for screen readers since colour
alone carries the state.

## D12. Voice: overhaul instead of tuning (2026-10-28)

**Evidence.** After three rounds of fixes (echo guard, Whisper hallucination filter, stricter
barge rule) live voice was worse: browser STT heard nothing, server STT trimmed the first
words ("One hundred twenty dollars" → "20$"), and replies took about 3.5 s.

**Diagnosis.** The browser's own speech (`speechSynthesis`) is not an echo-cancellation
reference in Chrome, so the agent heard itself; every guard against that echo also ate real
speech. Batch Whisper per clip and silence-only turn detection add latency and errors.

**Chosen** (`docs/VOICE_PLAN.md`): Pipecat inside the FastAPI app, WebRTC audio (so echo
cancellation works), Deepgram Flux (streaming STT with model-based end-of-turn) and Aura-2
(streaming TTS), free Cloudflare TURN for the hosted demo. The orchestrator stays the only
decision maker; "what the rep heard" comes from audio actually played. A measurement harness
and a spike come first (Phase 62), with a go/no-go.

**Alternatives rejected.** Staying free (Groq Whisper + browser speech, rebuilt client):
cannot reach production latency or reliable echo cancellation. Our own WebSocket pipeline:
more code and risk for the same result. Speech-to-speech models: would let a model decide
moves, against the core rule.

**Phase 56 handling.** Merged without its browser voice tuning (reverted); the server-side
Whisper filter, stale-speech fix, unheard-turn rollback and fragment merge stay.

## D13. The engine explains a miss and answers what-if (Phase 60, 2026-10-28)

**Built.** `feasibility/explain.py` returns why a price does not fit: reason (`floors`,
`segments`, `cash_short`, `fee_short`, `horizon`), the binding date, the exact shortfall and
the closest payment count. `feasibility/whatif.py` answers "would this term change make it
fit" on a shared `Problem`, so the agent's term suggestions no longer rebuild and rescan.

**Checks.** The shortfall is exact: added as a credit on the binding date it makes the
oracle feasible, one cent less does not (400+ small cases). The agent's term suggestions are
unchanged on all 165 recorded calls (0 differences), and faster (p50 3.2 → 2.1 ms per turn,
cold 13 → 1.6 ms). The explanation is private: operator trace only, added to the leak
blocklist, never an NLG fact, never on the rep stream.

**Not done by choice.** The engine can test structure and segment changes, but the agent's
policy does not propose them yet.

## D14. Keep the code-side number check; read back what it cannot confirm (2026-10-29)

**Question.** After the first Pipecat voice calls misread spoken amounts ("hundred and twenty
dollars" → $20; "a hundred and twenty dollars" not understood, then a handoff), should the agent
drop the code-side number reader and trust the LLM's reading?

**Chosen.** Keep it, fix the reader for spoken forms (Flux returns words, Whisper returned digits),
and add a fallback: a value the model read but the code cannot confirm is read back to the rep
("Just to confirm, a $120 minimum payment?") instead of re-asking.

**Why.** The reader does two jobs: short answers skip the LLM (about 1.5 s saved per turn), and
every amount the model extracts is confirmed against the rep's own words, which guards against
invented or misread money. The read-back keeps the model's reading useful without ever acting on
an amount the rep has not confirmed.

## D15. Voice turn-taking: pause on voice, decide by words (Phase 65, 2026-10-29)

**Barge-in: pause on local VAD, then decide by words (user decision).** Asked with numbers:
the agent stopped about 1.5 s after a real interruption (words needed first). Options were
to accept that, stop on any voice (fast, but every "mm-hmm" and echo cuts the agent off),
or pause on voice and let Flux's words decide. Chosen: pause. Silero VAD runs on the server
only while the agent talks. A barge stops and drops the rest; a backchannel, or nothing
within 0.6 s of quiet, resumes where it paused. Cost: a short hiccup on "mm-hmm".

**Filler only when the reply is really late (measured).** A filler 1.5 s after the final
made most LLM turns (reply 1.5 to 2.9 s after the final) wait behind it, and its silence
invited the rep to speak over the late reply. It now starts 3.0 s after the final.

## D16. Less sensitive pause; new voice becomes the default (2026-10-29/30)

**Question.** On the user's laptop the pause fired on background noise and speaker echo, so
the agent stuttered. Latency was fine. Reduce sensitivity, and at what cost?

**Chosen.** The pause detector (not Flux) needs Silero confidence 0.75, loudness 0.7 and
0.3 s of speech, and listens only while agent audio is actually playing. All three are
settings (`VOICE_VAD_CONFIDENCE`, `VOICE_VAD_MIN_VOLUME`, `VOICE_VAD_START_S`).

**Cost.** A real interruption now stops the agent about 0.66 s after speech starts (was about
0.46 s); backchannels paused 0 of 2 on the recorded clip. After the user's re-test
("it's better now") `VOICE_STACK=pipecat` became the default; `legacy` stays selectable
until Phase 67 removes it.

## D17. Legacy voice removed; text mode acks what the rep read (Phase 67, 2026-10-30)

**Question.** With the browser-speech client gone, who acks the agent's lines in a typed call?
The old client spoke every `say` with `speechSynthesis` (also in text mode) and acked it. The
WebRTC client never acks: the server acks what it played. So on main, with voice off, a typed
reply found the agent's last turn still unheard, barged it with nothing heard and rolled it
back (ADR 4), and a deal's agreement never committed.

**Chosen.** The browser acks while voice is off. The opening waits (the rep may still turn
voice on, and the server then speaks it); the rep's first typed line acks it first, and after
that each line is acked as it arrives. While voice is on the server keeps acking and ignores
client acks. The socket protocol is unchanged (`sentence_done` / `barge_in` stay for this,
scripts and tests).

**Alternatives rejected.** Server auto-ack whenever no voice session is attached: the opening
would be acked before voice can join, so turning voice on would never speak it, and the
socket tests that drive acks by hand would change meaning. Acking only on the rep's next
line: the agreement, eval and phase panels would lag one turn.

**Kept** (they started in the legacy era but the Pipecat path uses them): heard tracking,
rollback of unheard turns and fragment merge (Phase 56), `speech_dropped`, the voice budget,
the voice harness's WebRTC targets, and the frozen voice reports under `docs/eval/`.

## D17. Host the voice demo on AWS EC2, not Render free (2026-10-30)

**Question.** After Phase 66 the hosted voice connected through TURN, but the user heard slow,
choppy agent speech, late transcripts and missed words on Render. Render's free metrics showed
nothing. Upgrade Render, or move?

**Evidence.** Pipecat does per-frame audio work in Python (Opus decode/encode, Silero VAD,
resampling). The same build on an EC2 t3.small averaged 12% and peaked at 35% of 2 vCPU during
a 2-minute call (about 0.7 of a CPU), against Render free's 0.1 CPU. The user's call there
"felt as good as on my laptop".

**Chosen.** One t3.small in the account's Region (ap-southeast-2), on the AWS free plan's
credits (about $23 a month): Caddy for HTTPS on an sslip.io name, systemd, SSH from the owner's
IP only, no inbound UDP (stateful security group plus the browser's TURN relay), IMDSv2. The
server's own ICE is STUN only (`VOICE_SERVER_TURN=false`): it has a public IP, and aioice's
TURN channel bind against Cloudflare failed with 401. Render stays as a text-only mirror.
Details: [`docs/DEPLOY_AWS.md`](../DEPLOY_AWS.md).

## D18. Instant ack in a neutral word; cache only number-free sentences (Phase 70, 2026-11-07)

**Question.** With Claude Haiku reading the rep, the first reply audio was 2.5 s p50 after the
rep stopped (end of turn 0.5 s, NLU 1.3 s, TTS first byte 0.35 s, playback 0.3 s). How to make
the agent sound present sooner without letting a model or a canned line decide anything?

**Chosen (user, 2026-11-07).** (1) An instant ack on model turns: a short pre-rendered sound in
the agent's own Aura-2 voice, "Mm-hm." or "Right.", never "Okay", "Sure" or "Got it": after a
price those can sound like agreeing to it (the premature-commitment guard exists for that). It
commits nothing: no sentence id, no heard tracking, no transcript line, no rollback effect. (2)
An exact-text audio cache for sentences with no digits and no number words. Sentences with
numbers always go to live TTS; no splicing of cached words around a live number.

**Why.** The ack moves the first sound to about 0.75 s (warm p50, was 2.5 s) without touching
what the agent decides or says. Number splicing would save more on these calls but joins two
recordings mid-sentence (audible seams, prosody that no longer fits) on exactly the words that
matter most; the cache only serves lines that are word-for-word repeats.

**Rules added while measuring.** No ack in a turn that interrupted the agent (the rep is usually
still talking: "Hold on. That is too low..."). Evidence:
[`docs/eval/voice_ack_20261107/summary.md`](../eval/voice_ack_20261107/summary.md).

## Process choices

- Each phase runs in its own git worktree with a paste-ready prompt; phases that touch the
  same files run in sequence.
- Every merge runs the same checks: ruff, the full test suite, the fast suite without
  `.env` (as CI runs), the offline policy eval, and the web build for UI phases.
- UI phases wait for a manual browser review before merge.
- Evidence first: measure, then decide scope (D4, D7).

## Open items

- Completeness of the take-home family without tiers: conjecture only.
- Adversarial tail over 150 ms (57b, 59); proportional fee inexact and slow (opt-in).
- Voice thresholds: tuned on injected audio and one laptop-speaker test by the user (D16); no wider device check.
- Hosted demo voice needs TURN and a Deepgram key on Render (Phase 66); until then the hosted demo is text only.
- haggling_rep reads back "no special payment tiers" after the deal is sent (policy bug).
- Sim creditor rejects a legal confirm whose count differs from its own plan.
