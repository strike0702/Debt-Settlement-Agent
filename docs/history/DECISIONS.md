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
- Voice thresholds need a run on real speakers.
- haggling_rep reads back "no special payment tiers" after the deal is sent (policy bug).
- Sim creditor rejects a legal confirm whose count differs from its own plan.
