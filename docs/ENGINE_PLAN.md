# Engine v2 plan (Phases 55–61)

## Context

Retape AI wants an in-person interview after Shreyan's outreach email. The email leads on the
feasibility engine (the take-home) and on "code decides every move, the engine computes every
number". The engine is still the take-home code, vendored read-only, with three weaknesses
Shreyan named himself in the take-home README and interview notes:

1. **Candidate family is structured, not exhaustive.** Pinned early levels plus a Diophantine
   scan on the last two staircase levels. It can call an offer infeasible when a legal schedule
   exists, and then the agent walks away from a deal it could make. Part 2 rescue minima are
   relative to the same family.
2. **Ranking serves the firm only.** Fee timing first, then fewest payments. On easy_deal the
   agent offers "2 payments totaling $500" though the rep allows 8 (Phase 25 open issue; memory
   note: engine redesign deferred to now).
3. **No explanation.** Infeasible is just `false`. The agent's term alternatives
   (`app/agent/term_alts.py`) brute-force up to 24×100 engine runs to find which term change
   helps, and the trace cannot say why a price does not fit.

User decisions (2026-10-22): do exact search + proof, client-first ranking, why-infeasible +
what-if; the engine becomes an **owned v2** in this repo; add **one cleanup phase** for
leftovers; about two weeks, so the full set plus docs and evidence. No "exact max % to the
basis point" phase (not chosen); speed is only a budget the exact search must meet.

## What is unfinished today (from docs/history/PROGRESS.md)

- Status table: Phases 16, 17, 18 say "not done". 16 and 17 were superseded by 24a (LLM-only
  arm) and 21 (latency); 18 (replay CLI) never shipped. Fix the table, don't build 18.
- Leftovers for the cleanup phase:
  - `app/agent/reasons.py`: "two small steps" wording for `step` / `rep_held` (equal steps since
    P50); `alt_first_payment_date` short reason says "Their start date" when it was assumed;
    `alt_min_payment_cents` / `alt_max_payments` blame the rep when the term was assumed.
  - NLG bank: "1 payments" ("We could set 1 payments totaling $500", late_start_date).
  - `fixtures/scenarios/no_space/meta.json` says `expected: no_deal`, autoplay gives `escalate`.
  - haggling_rep card text ("holds, steps up, offers once more, then accepts") does not match
    autoplay (deal on the first step).
  - `.env.example` lists the ignored `CLOSE_GAP_BP` and lacks the three P45 settings.
  - Web: `mic.ts` returns to listening after a handoff (only `END` → `call_over`); dead
    `NO_DEAL_WRAP` label in `traceStory.ts`; COUNTER_TERMS row shows the rep's ask as its key
    number; phone card row does not scroll the selected card into view; `web/README.md`
    screenshots predate P35/36/48 (`console-1440-operator.webp` shows "step up to 51%").
- Not scheduled (listed only): human judge check is done (P50); native tool calling for the
  ReAct eval arm; NLU "N% balance" asks; bare amounts after "take your time" (P54 a); hosted
  demo redeploy (user step).

## Revision 2026-10-25 (after Phase 55)

Phase 55's gap report (`docs/eval/engine_gap_20261023/`) found **no** miss by the take-home
candidate family: 85,000 small random cases and a random-cap sweep (k ≤ 8) gave 0 false
infeasible, 0 worse Φ\*, 0 rescue over-estimates. User decision: hunt large cases first.
Phase 57 is now that hunt (prompt below). The original exact-search phase becomes 57b; its
scope, and whether 58 stays, are decided from 57's result. 59–61 are unchanged for now.

## Revision 2026-10-27 (after Phase 57)

Phase 57 (`docs/eval/engine_gap_20261025_full/`) found validated misses: staircases with
≥ 3 levels, min-payment tiers and tight cash (0 in golden and 20,000 random cases; 25 in
20,000 adversarial; 112 structural witnesses; worst a $161.78 lump asked when none is
needed). Proved exact: even, balloon, ≤ 2 levels. User decision: one phase, **57b**, ports
the reference's dominating family for staircases and makes rescue exact (old 58 merged in).
Note for 59: "minimise the largest payment" is not monotone in prefix sums, so client-first
needs its own search beyond the fee-first family.

## Approach

### The formulation that makes exact search possible

Fix the first payment date and the cadence `t_1..t_M`. Simulate the ledger once with no creditor
payments and no fee: base balance `B0(v)`. A k-payment vector `p` with prefix sums `P_j` is
feasible iff for every window j the base window minimum minus `P_min(j,k)` and the bank fees
stays ≥ 0, and the last window still covers the fee `F`. That is: **prefix sums under a fixed
cap vector**, caps computed once per (client, rules, first payment date) and independent of the
settlement %. The fee-first objective (lexicographic Φ\*, suffix-min closed form) only gets
better when prefix sums get pointwise smaller. So:

- Feasibility and fee timing depend on the vector only through its prefix sums.
- Balloon allowed: the floors-pinned balloon has pointwise-minimal prefix sums, so it is optimal
  and a one-line certificate when it fails.
- Staircase (≤ `max_segments` levels): search compositions × levels by branch and bound, prune
  any partial vector whose prefix sum already passes its cap or whose remaining sum cannot fit
  the floors/segments. The old generator's miss is the divisibility pin (early levels fixed at
  their floor); the new search lifts early levels as needed.
- Even: one vector per k, unchanged.
- Speed: caps and the base simulation are reused across all 100 bp of an affordability scan and
  across term what-ifs, so per-bp work is only the vector search.

Proof of completeness is empirical plus a written argument: a brute-force oracle that enumerates
every legal non-decreasing vector on small instances, run on thousands of seeded random cases,
must agree with the engine on feasibility, Φ\*, and Part 2 minima. The independent validator
(`app/adapter/validator.py`) checks every schedule.

### Owned v2, not a rewrite of the callers

`feasibility/` stays the package name (11 importers in `app/`, `sim/`, `eval/`). Phase 55
removes the vendored/read-only status, brings it under ruff, takes settlement % as integer bp
(no float in `Offer`), and adds a `Problem` object (precomputed base sim + caps). The old
`evaluate_offer(client, offer, rules)` stays as a compatibility wrapper and keeps the take-home
behaviour as objective `fee_first`. The old candidate generator is kept as
`feasibility/legacy.py` until Phase 61 so the gap report can compare.

### Objectives and fee policy (Phase 59)

- `fee_first` (take-home): Φ\* lexicographic, then the old tie-breaks. Kept bit-identical except
  where exact search finds a better Φ\*.
- `client_first` (agent default): maximise post-fee cushion (minimum window slack), then
  minimise the largest creditor payment, then Φ\*, then fewer payments, then the deterministic
  tie-break. Exact key written in docs before coding; the worker may adjust the order only with
  a measured reason recorded in PROGRESS.
- Optional `fee_policy = "proportional"`: fee collected by `t_j` ≤ `F × P_j / S` (rounded down),
  modelled on the FTC Telemarketing Sales Rule advance-fee provision. Still a suffix-min closed
  form (the cap is non-decreasing). Default stays `front_loaded`. The worker checks the
  citation before writing it in docs and says it is a model, not legal advice.

### Explain and what-if (Phase 60)

- `explain(problem, bp) -> Infeasibility` with a reason code (`floors`, `segments`,
  `cash_short`, `fee_short`, `horizon`), the binding date, the shortfall in cents, and the k it
  is closest at. All of it is PRIVATE (client finances): operator trace only, never NLG, never
  the rep stream.
- `what_if(problem, change) -> max_bp / feasible set` and `unlocking_changes(problem, ask_bp)`
  for first payment date, min payment, max payments, structure, segments. `term_alts.py` keeps
  its order and ranking rules but calls these instead of rebuilding rules and rescanning.

## Phases

| # | Name | Main paths | Depends on |
|---|---|---|---|
| 55 | Engine v2 foundation: owned package, bp input, `Problem`, golden corpus, oracle, gap report | `feasibility/`, `tests/engine/`, `scripts/engine_gap_report.py`, `pyproject.toml`, `CLAUDE.md`, `app/adapter/engine_adapter.py` (bp only) | — |
| 56 | Cleanup leftovers | `app/agent/reasons.py`, `config/nlg_bank.json` (+ bank build), `fixtures/scenarios/{no_space,haggling_rep}`, `.env.example`, `web/src/lib/{mic,traceStory}.ts`, phone card row, `web/README.md` + shots, PROGRESS status table | — (parallel with 55, 57) |
| 57 | Large-case hunt: does the take-home family miss anything at full size? (replaces the original 57, see Revision) | `tests/engine/reference.py` (new), `scripts/engine_gap_report.py`, `docs/eval/engine_gap_*` | 55 |
| 57b | Exact staircase search + exact rescue (old 58 merged in) | `feasibility/search.py` (new), `shapes.py`, `engine.py`, `rescue.py`, `tests/engine/` | 57 |
| 59 | Objectives + fee policy | `feasibility/objectives.py` (new), `scoring.py`, adapter, `app/config.py`, validator (proportional rule), policy CONFIRM wording if counts change, eval | 57b |
| 60 | Explain + what-if, term_alts on the engine, trace line (UI, manual review) | `feasibility/explain.py`, `feasibility/whatif.py` (new), `app/agent/term_alts.py`, `app/agent/orchestrator.py` (`_engine_context`), `app/voice/views.py`, `app/schemas/events.py`, `web/src` trace | 59, 56 merged |
| 61 | Docs + evidence | `docs/ENGINE.md` (new), `docs/DESIGN.md` ADR 5 + new ADR, README engine section, `docs/eval/engine_v2_<date>/`, remove `feasibility/legacy.py` | 60 |

### Execution strategy

- **Wave 1 (parallel worktrees):** 55 ∥ 56. 55 touches only `feasibility/`, `tests/engine/`,
  the adapter's Offer construction, `pyproject.toml` and `CLAUDE.md`; 56 touches copy, fixtures,
  web and docs. No shared files. One rule: 56 must not edit `CLAUDE.md` or `pyproject.toml`.
- **Wave 2:** 57 after 55 merges (56 may still be running; still disjoint).
- **Waves 3–6 sequential:** 57b → 59 → 60 → 61. All change the engine's public API in
  `feasibility/engine.py` and the adapter, so parallel branches would conflict. 60 needs 56
  merged (both touch `traceStory.ts`).
- 56 and 60 have UI changes: the user checks them in the browser before merge (screenshots in
  `../dsa-orch-tools/p<N>-shots/`, local run command in the reply).
- After merge of each wave: orchestrator runs `uv run pytest -q`, `uv run ruff check .`, the
  oracle eval, and saves the log as `../dsa-orch-tools/gates-main-p<N>.log`.

## Phase prompts

On approval, this plan is copied to `docs/ENGINE_PLAN.md` and each prompt below is saved as
`../dsa-orch-tools/prompt-p<N>.txt` with the standard worktree header (branch `phase-<N>`, no
merge, DEFERRED lines, `PHASE <N> DONE <sha>` last line) and this tail:

> NOTIFICATIONS: do not use Monitor or background waits that wake you for progress (per-N
> calls, per-scenario). Run long commands in the foreground; wake only on process exit or error.

Common to all engine phases: money in integer cents, settlement in integer bp, no float on any
money path; every new module opens with a docstring per CLAUDE.md; seeded stdlib `random` for
generated cases (no new dependency); no paid LLM calls (oracle eval only:
`uv run python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7`
with `.env` moved aside and restored).

---

### Phase 55 — Engine v2 foundation

You are implementing Phase 55 (engine v2 foundation) for Debt-Settlement-Agent. Read CLAUDE.md,
docs/ENGINE_PLAN.md (Context, Approach), docs/history/PROGRESS.md (Environment facts, Deviations,
`app.adapter.engine_adapter` interface), then every file in `feasibility/`,
`app/adapter/engine_adapter.py`, `app/adapter/validator.py`, `tests/engine/`.

Scope: no behaviour change. Every engine output on every input stays identical.

1. **Owned package.** Remove `feasibility` from ruff `extend-exclude`; fix lint without changing
   logic. Add module docstrings in the repo style. Update CLAUDE.md: replace "`feasibility/` is
   read-only except for bug fixes" with "`feasibility/` is the owned engine v2: every behaviour
   change ships with an oracle or golden test and a PROGRESS note". Update the memory-backed
   note in PROGRESS Deviations (ruff exclude gone).
2. **bp input.** `Offer.settlement_bp: int` replaces `settlement_pct: float`; `offer_total_cents`
   uses `Decimal(bp) / 10000`. `program_fee_pct` becomes `program_fee_bp: int` in
   `CreditorRules` (adapter converts `firm.program_fee_pct` once, exactly, rejecting values that
   are not whole bp). Keep JSON loaders accepting the old float keys for the take-home fixtures.
3. **`Problem`.** New `feasibility/problem.py`: `build_problem(client, rules, first_payment_date)
   -> Problem` holding cadence, M, k_max, floors, the base simulation (no creditor payments, no
   fee), base window minima and pre-window minimum. `evaluate_offer` builds one internally;
   new `evaluate(problem, bp) -> Result` uses it. Adapter `affordability` builds the Problem
   once per scan and calls `evaluate(problem, bp)` 100 times. Document the prefix-cap
   identity in the docstring and **test it**: for every candidate on the golden corpus, the
   per-candidate simulation equals base sim minus prefix payments and bank fees.
4. **Golden corpus.** `tests/engine/golden/`: the 5 engine fixtures, the 7 scenarios × 100 bp
   (true rules), and 600 seeded random cases (generator in `tests/engine/gen_cases.py`: varied
   horizon 3–24 months, ledgers with committed debits, tiers, token limits, all three
   structures, segments 1–4, mid-month and EOM start dates). Store the current `to_dict()`
   output compactly (one JSONL, aim < 3 MB). `test_golden.py` asserts identical output.
5. **Brute-force oracle.** `tests/engine/oracle.py`: on small instances (S ≤ ~400 cents scaled,
   k_max ≤ 6, M ≤ 8), enumerate every vector legal under ASSIGNMENT §5 rules 1–9 (not the
   generator's family), simulate with the suffix-min fee, and return feasibility, the
   lexicographically best Φ\*, and Part 2 minima by binary search on L and X. Write the rules
   from the problem statement independently of `shapes.py`; cross-check legality with
   `app/adapter/validator.py` on every oracle schedule.
6. **Gap report.** Copy the current generator to `feasibility/legacy.py` (frozen, used only by
   the report and later golden comparisons). `scripts/engine_gap_report.py --cases 5000 --seed 1`
   compares legacy vs oracle on small random cases: false-infeasible count, worse-Φ\* count,
   rescue over-estimates, with 3 minimal examples each. Save output to
   `docs/eval/engine_gap_<date>/summary.md`. This is evidence; do not fix anything here.

Acceptance: golden test identical; prefix-cap identity test green; oracle agrees with legacy on
every legacy-feasible case's legality; gap report written; affordability scan median on the
demo fixture no slower than the PROGRESS figure (8.3 ms) +20%; pytest, ruff, oracle eval
thresholds PASS with `summary.json` identical to main except latency.
Commit: `phase 55: engine v2 foundation — owned package, bp input, Problem, golden corpus, oracle, gap report`.

---

### Phase 56 — Cleanup leftovers (parallel with 55/57)

You are implementing Phase 56 (leftover cleanup) for Debt-Settlement-Agent. Read CLAUDE.md,
docs/ENGINE_PLAN.md ("What is unfinished today"), PROGRESS entries for Phases 45, 48, 50, 50b,
54. Do not touch `feasibility/`, `tests/engine/`, `pyproject.toml`, `CLAUDE.md`, the adapter, or
policy decisions. Copy rules: sentence case, plain words for people outside debt settlement.

1. `reasons.py`: `step` / `rep_held` describe equal steps (one step halfway, then the limit);
   `alt_first_payment_date` / `alt_min_payment_cents` / `alt_max_payments` full and short
   sentences do not blame the rep when the term was assumed (use the existing assumed
   detection; keep keys and placeholders, the pinned test must pass).
2. NLG bank: singular count ("1 payment of $500" / "a single payment of $500"); fix through the
   bank build script, not by hand-editing generated output; test that no rendered bank line
   says "1 payments".
3. Fixtures: `no_space/meta.json` expected matches autoplay (`escalate`) with a one-line reason
   in PROGRESS; haggling_rep card text matches what autoplay does.
4. `.env.example`: drop `CLOSE_GAP_BP`, add the P45 settings with one-line comments.
5. Web: mic goes to `call_over` on any handoff ending too; remove the dead `NO_DEAL_WRAP` label;
   COUNTER_TERMS row key number shows the term we asked for, not the rep's ask; selected phone
   card scrolls into view. Tests for each.
6. Docs: PROGRESS status rows 16/17 → "superseded by 24a / 21", 18 → "dropped"; retake
   `web/README.md` screenshots and alt text.

UI review: save screenshots to `../dsa-orch-tools/p56-shots/`, give the local run command, and
do not consider the phase mergeable until the user approves in the browser. Design refs:
`../dsa-orch-tools/design-refs/`.
Acceptance: pytest, ruff, web CI chain (`npm ci && npm run gen:types && git diff --exit-code
src/types/events.ts && npm run typecheck && npm run lint && npm test && npm run build`), oracle
eval PASS. Commit: `phase 56: leftover cleanup — reason copy, singular counts, fixtures, env example, mic, trace labels, screenshots`.

---

### Phase 57 — Large-case hunt

You are implementing Phase 57 (large-case hunt) for Debt-Settlement-Agent. Read CLAUDE.md,
docs/ENGINE_PLAN.md (Revision 2026-10-25, Approach), PROGRESS Phase 55 handoff,
`docs/eval/engine_gap_20261023/{summary,notes}.md`, `feasibility/` (problem.py, shapes.py,
scoring.py, legacy.py), `tests/engine/{oracle,gen_cases}.py`, `scripts/engine_gap_report.py`.

Question to answer: does the take-home candidate family (`feasibility/legacy.py`) ever give a
worse answer than the full legal set (ASSIGNMENT rules 1–9) on full-size cases (k up to 24,
offer totals in the thousands of dollars, up to 4 levels)? Worse = false infeasible, later
fee (worse Φ\*), or a higher rescue amount. **Evidence only: no engine behaviour change.**
Nothing under `feasibility/` changes except a bug fix with its own test.

1. **Reference solver** `tests/engine/reference.py`: exact over the full legal set at full
   size, using the prefix-cap identity (`feasibility/problem.py`). For each k and level
   composition, search level values by branch and bound with prefix-cap and remaining-sum
   pruning; it must not reuse the legacy family's pins. Write its exactness argument in the
   module docstring (what is pruned and why that cannot lose the optimum). It is a test
   tool, so clarity beats speed, but a full-size case should finish in seconds.
2. **Check the reference:** it agrees with `tests/engine/oracle.py` on every small case of
   the Phase 55 sets (seed 1, 5,000 cases, plus one 10,000-case seed): feasibility, Φ\*,
   both rescue minima. Any disagreement is a reference bug: fix it before going on.
3. **Hunt** (extend `scripts/engine_gap_report.py`, e.g. `--mode full`):
   - the 600 full-size golden cases and the 7 scenarios × 100 bp (true rules);
   - ≥ 20,000 seeded full-size random cases;
   - an adversarial generator aimed at the legacy pins: max_segments 3–4, run lengths with
     a shared factor (e.g. 2 and 4, 3 and 6), offer totals not divisible by them, tiers and
     token limits that split runs, tight cash (caps just above the cheapest legal prefix
     sums), mid-month and EOM starts; ≥ 20,000 cases.
   For each miss, keep a validator-checked witness and shrink it (fewer payments, smaller
   amounts) while it still misses. Report counts by kind and structure, the 3 smallest
   witnesses per kind, and run time.
4. **Read the result** in `docs/eval/engine_gap_<date>_full/notes.md`:
   - misses found: how often, which rule combination, and the mechanism (which pin loses it);
   - none found: the strongest statement the data supports, plus a proof sketch of why the
     family is complete for fee-first (or the exact gap in that sketch). Do not claim a
     proof that is not one.
   End with a recommendation for 57b (exact search in the engine: needed / not needed for
   fee-first; whether 59's client-first ranking still needs it) and for 58 (rescue).

Acceptance: reference agrees with the oracle on every small case; report and notes written;
pytest (new tests fast, big runs only in the script or under the `slow` marker), ruff,
oracle eval thresholds PASS with `summary.json` identical to main except latency.
Commit: `phase 57: large-case hunt — exact reference solver vs the take-home family at full size`.

---

### Phase 57b — Exact staircase search and exact rescue (57b + 58 merged)

You are implementing Phase 57b (exact staircase search and exact rescue) for
Debt-Settlement-Agent. Read CLAUDE.md, docs/ENGINE_PLAN.md (Revision 2026-10-27, Approach),
PROGRESS Phase 55 and 57 handoffs, `docs/eval/engine_gap_20261025_full/{summary,notes}.md`,
`tests/engine/reference.py` (the dominating family and its exactness argument),
`tests/engine/{oracle,hunt,gen_cases}.py`, `feasibility/` (problem.py, shapes.py, scoring.py,
engine.py, rescue.py, legacy.py), `scripts/engine_gap_report.py`.

Phase 57 proved the take-home family exact for even, balloon and staircases with ≤ 2 levels,
and found validated misses for staircases with ≥ 3 levels, min-payment tiers and tight cash
(the pin on levels v_1..v_{m-2}): false infeasible, worse Φ\*, and rescue over-asks (worst:
a $161.78 lump asked when no extra money is needed). Fix exactly that, nothing more.

1. **Staircase search in the engine.** New `feasibility/search.py` (or in `shapes.py` if
   clearer): the reference's dominating family for staircases — per composition, early
   levels in the box `[lo_l, lo_l + c_m/gcd(c_l, c_m))`, last two levels in closed form —
   with branch and bound on the fee-first key. Even, balloon and ≤ 2-level staircases keep
   today's code paths (proved exact; their golden output must not change). Carry the
   exactness argument into the module docstring; the engine must not import from `tests/`.
2. **Exact rescue (old Phase 58).** `rescue.py` computes L\* (and its latest date) and X\*
   as the minimum over the same dominating family (monotone in prefix sums, so no binary
   search). Guardrails, reason texts and `within_guardrail` semantics unchanged.
3. **Speed.** Budget: affordability scan p50 ≤ 2× the Phase 55 figure on the same machine,
   and max ≤ 150 ms per 100-point scan on the 7 scenarios, the demo fixture and the golden
   full-size cases. The 57 reference took up to ~2.7 s on adversarial cases; reuse the
   `Problem` caps across bp, memoise per (k, composition), and prune by the best key found
   so far. Report before/after in `docs/eval/engine_v2_<date>/bench.md`. If an
   adversarial case cannot meet the budget, record it and keep correctness; do not trim
   the family.
4. **Tests.**
   - Engine vs `tests/engine/reference.py` on the Phase 57 sets (golden, 20,000 random,
     20,000 adversarial, the 112 structural witnesses): feasibility, Φ\*, L\*, X\* equal on
     every case (big sets under the `slow` marker; ≥ 2,000 in the fast suite including every
     known witness).
   - Engine vs oracle on ≥ 5,000 small infeasible cases for L\*/X\*, and strict minimality
     (L\*−1, X\*−1 infeasible) on the fixtures and the witnesses.
   - Every produced schedule passes `app/adapter/validator.py`.
   - Golden corpus: no case goes feasible → infeasible, Φ\* never worse, rescue never
     higher; regenerate with `--update`, list every changed case with its kind.
   - The gap report re-run on main reports 0 misses for the engine (keep `legacy.py` for
     the comparison until Phase 61).
5. **Agent effect.** Oracle eval thresholds PASS; list any metric or per-call change and
   why (expected: none, since scenarios showed no miss). Note in PROGRESS that "legacy is
   complete without tiers" stays a conjecture.

Commit: `phase 57b: exact staircase search and exact rescue minima, verified against the reference`.

---

### Phase 59 — Objectives and fee policy

You are implementing Phase 59 (schedule objectives) for Debt-Settlement-Agent. Read CLAUDE.md,
docs/ENGINE_PLAN.md (Objectives and fee policy), PROGRESS 57/57b handoffs, `feasibility/search.py` (the 57b
search and its bounds), `feasibility/scoring.py`, `tests/engine/{oracle,reference}.py`,
`app/adapter/engine_adapter.py`, `app/adapter/validator.py`, `app/config.py`,
`app/agent/policy/moves.py` (CONFIRM wording), `docs/DESIGN.md` ADR 7.

1. `feasibility/objectives.py`: `Objective = fee_first | client_first` as explicit sort keys,
   written in docs/ENGINE.md before code. The search returns enough per-k candidates (or
   Pareto set on prefix sums) that `client_first` is exact, not "best of the fee-first
   winners"; extend the oracle to rank by each objective and require agreement.
   Known from 57/57b: the `Search` bounds are valid for keys that are monotone in prefix
   sums (feasibility, Φ, cushion, L, X). "Minimise the largest creditor payment" is **not**
   monotone in prefix sums, so it needs its own bound (e.g. a lower bound on the last run
   level from the remaining sum) or a full-box search for that key; prove the bound in the
   docstring and check it against the oracle and `reference.py`.
   Before fixing the key order, measure whether "minimum post-fee window slack" is
   degenerate under the front-loaded fee (often 0, because Φ\* fills windows to their
   suffix minimum). If it is, define cushion on a basis that is not (e.g. the lowest balance
   with the fee placed, or with a reserved buffer) and record the measurement and the
   choice in docs/ENGINE.md and PROGRESS.
   Speed budget as 57b: scan p50 ≤ 2× the 57b figure, max ≤ 150 ms on scenarios, demo and
   golden; report in `docs/eval/engine_v2_<date>/bench.md` (append a section).
2. `fee_policy = front_loaded | proportional` on `Problem`; proportional caps Φ\_j at
   ⌊F·P_j/S⌋ inside the suffix-min. Validator gains a `fee_proportional` rule checked only when
   the policy is on. Oracle covers both policies.
3. Settings: `ENGINE_OBJECTIVE` (default `client_first`) and `ENGINE_FEE_POLICY` (default
   `front_loaded`) in `app/config.py` and `.env.example` (add them as a new block at the end
   of `.env.example`: Phase 56, not yet merged, also edits that file); the affordability
   cache key includes both.
   `evaluate_offer` (take-home wrapper) stays `fee_first` + `front_loaded`; golden corpus under
   that mode unchanged.
4. Agent effect: easy_deal now offers a plan that uses the allowed payment count when that
   leaves more cushion; check CONFIRM read-back for every shape and count (no "1 payments",
   staircase "starting at … rising to …"). Oracle eval PASS; add an eval metric
   `min_cushion_cents` (PRIVATE, report only) comparing objectives on the 100 calls.

Commit: `phase 59: fee-first and client-first objectives, proportional fee option`.

---

### Phase 60 — Explain and what-if (UI phase, manual review)

You are implementing Phase 60 (why-infeasible and what-if) for Debt-Settlement-Agent. Read
CLAUDE.md, docs/ENGINE_PLAN.md (Explain and what-if), PROGRESS 59 handoff and Phases 35/36/48
(trace design), `app/agent/term_alts.py`, `orchestrator._engine_context`, `app/voice/views.py`,
`app/schemas/events.py`, `web/src/components/DecisionTrace.tsx`, `web/src/lib/traceStory.ts`.

1. `feasibility/explain.py`: `explain(problem, bp, objective) -> Infeasibility | None` (reason
   code, binding date, shortfall cents, closest k). Tests: one per reason code; oracle check
   that the shortfall is exact (adding it as a lump on that date makes the oracle feasible
   for that k).
2. `feasibility/whatif.py`: `what_if(problem, change)` and `unlocking_changes(problem, ask_bp)`
   reusing the base simulation where the change allows (min payment, max payments, structure,
   segments); first-payment-date changes rebuild the Problem.
3. `term_alts.py` calls the engine API; same order (date → min payment → max payments), same
   ranking, same `terms_countered` skip. Test: on all 7 scenarios and the 100 eval calls the
   chosen alternative equals the Phase 59 result, or each difference is listed and justified;
   record engine_ms before/after on the term-alt turns.
4. Trace: `TurnTrace.infeasible_reason` (optional; schema + `events.ts` regenerated), filled
   on the operator stream only; `views.py` strips it from the rep stream (test like the
   existing private-field test); the leak scan blocklist adds the shortfall. Operator trace
   line, plain words: e.g. "At 52% the client's savings run short by $140 on March 31."
   Never in NLG input (test).

UI review: screenshots in `../dsa-orch-tools/p60-shots/`, local run command, user approves
before merge; design refs in `../dsa-orch-tools/design-refs/`.
Acceptance: pytest, ruff, web CI chain, oracle eval PASS. Commit:
`phase 60: why-infeasible and what-if in the engine; term alternatives and trace use them`.

---

### Phase 61 — Docs and evidence

You are implementing Phase 61 (engine v2 docs) for Debt-Settlement-Agent. Read CLAUDE.md,
docs/ENGINE_PLAN.md, PROGRESS 55–60 handoffs, docs/ENGINE.md draft, README, docs/DESIGN.md,
docs/eval/README.md.

1. `docs/ENGINE.md` for an outside reader: problem, prefix-cap formulation, exact search and
   dominance argument, objectives and fee policy, explain/what-if, verification (oracle counts,
   validator, golden), the gap report numbers (what the take-home missed), benchmarks.
2. DESIGN ADR 5 updated with the curve finding from 57; new ADR "Engine v2: exact search,
   objectives, explanations". README engine paragraph + links. docs/eval index rows.
3. Remove `feasibility/legacy.py` and anything that only served the gap report (keep the
   frozen report). Golden test keeps working without it.
4. Re-run the oracle eval and the engine benchmark on main; freeze them under
   `docs/eval/engine_v2_<date>/`.

Acceptance: `tests/unit/test_docs.py` link check, pytest, ruff, oracle eval PASS. Commit:
`phase 61: engine v2 docs and frozen evidence`.

## Verification (whole plan)

- Every phase: `uv run pytest -q`, `uv run ruff check .`, oracle eval thresholds PASS, and the
  phase's own acceptance list; UI phases also the web CI chain and the user's browser check.
- Engine correctness: oracle agreement counts (Part 1 ≥ 20 000 cases, Part 2 ≥ 5 000, both
  objectives and both fee policies), validator on every produced schedule, golden corpus
  regression rules (never feasible → infeasible, Φ\* never worse under `fee_first`).
- End to end: run the app locally, autoplay easy_deal (plan uses the allowed count),
  late_start_date and rescue_escalate (trace shows the shortfall line on the operator view,
  absent on the rep view), and one typed call where the rep's ask is unreachable (term
  alternative still offered in the same order).
