# Feasibility engine v2

The settlement agent never invents a number. Every figure it may say comes from this
engine, `feasibility/`: can the client afford a settlement of X% of the balance, and if
so, which payment schedule should the creditor be offered? If not, how much extra
money would make it work, and why does it not fit?

The engine began as a take-home exercise (an earlier project of mine) and was vendored
into this repo read-only. Phases 55–61 (plan: [`ENGINE_PLAN.md`](ENGINE_PLAN.md)) made it
an owned package with an exact search, two ranking objectives, a fee policy option, and
explanations. This page is for an outside reader: what problem it solves, how it is
solved exactly, how that is checked, and how fast it is. The decisions behind each step,
with the evidence the user saw when making them, are in
[`history/DECISIONS.md`](history/DECISIONS.md) (D1–D13); this page does not repeat
them.

| Section | In one line |
|---|---|
| [1. The problem](#1-the-problem) | Rules a payment plan must obey, and the two questions asked |
| [2. Prefix caps](#2-the-prefix-cap-formulation) | One ledger simulation per client turns every rule about money into caps on prefix sums |
| [3. Exact search](#3-exact-search-and-the-dominance-argument) | A small dominating family plus branch and bound is exact |
| [4. Objectives and fee policy](#4-objectives-and-fee-policy) | Fee-first (default) or client-first; front-loaded or proportional fee; a per-firm cushion |
| [5. Explain and what-if](#5-explain-and-what-if) | Why a price does not fit, and which term change would make it fit |
| [6. Verification](#6-verification) | Oracle, reference, validator and golden corpus, with counts |
| [7. What the take-home missed](#7-what-the-take-home-missed) | The gap report numbers |
| [8. The affordability curve](#8-the-affordability-curve) | Feasibility is still not monotone in the price, so the agent still scans |
| [9. Benchmarks](#9-benchmarks) | Scan and evaluate timings, frozen |
| [10. Limits](#10-limits-and-open-issues) | What is not exact, not fast, or not proved |

All money is integer cents and every settlement percentage is integer basis points
(4500 = 45%). The engine converts with `Decimal(bp) / 10000` and rounds half up; no
float touches money.

## 1. The problem

**Inputs.**

- A *client*: a balance today and a ledger of future savings-account entries (monthly
  draft credits up to the end of the savings plan, and committed debits such as fees
  for other settled debts).
- A *creditor offer*: the balance owed, the original balance, and a settlement price in
  bp. The offer total is `S = round_half_up(bp/10000 × balance)`.
- *Creditor rules*: the most payments and terms, a minimum payment, min-payment *tiers*
  (from payment i on, at least m), a limit on how many payments may sit at the bare
  minimum ("token" payments), whether payments must be even, whether a final balloon
  is allowed, the most distinct payment amounts (`max_segments`), a bank fee per
  payment, and the debt firm's program fee (`F = round_half_up(fee_bp/10000 × original
  balance)`).
- A first payment date (default: end of the first draft's month). Payments fall on a
  monthly cadence `t_1..t_M` up to the end of the savings plan.

**A plan is legal** when (the take-home's binding rules 1–9): it has k ≤ the payment cap
payments on the first k cadence dates; they sum to exactly S; they never decrease; each
is at or above its floor (minimum, tiers, and minimum + 1 cent beyond the token limit);
even rules mean the as-equal-as-possible split; a staircase uses at most `max_segments`
amounts unless balloon rules allow a larger last payment; each payment date also takes
the bank fee; the program fee F is collected on cadence dates by `t_M`; and the client's
balance never goes below zero on any day.

**Part 1** (some legal plan is feasible): return the best one. The take-home ranking,
kept as the default, collects the firm's program fee as early as the money allows.
**Part 2** (none is): return the smallest one-off lump sum `L*` that makes some plan
feasible, with the latest date it can arrive, and the smallest uniform increase `X*` to
every future monthly draft, each with a guardrail (lump at most 65% of S; increase at
most the greater of $100 and 40% of the draft).

Public entry points (`feasibility/engine.py`):

| Call | Returns |
|---|---|
| `build_problem(client, rules, fpd, *, creditor_balance_cents, original_balance_cents, fee_policy, min_cushion_cents)` | A `Problem`: everything that does not depend on the price |
| `evaluate(problem, bp, objective="fee_first")` | `Result`: schedule rows, or Part 2 rescue amounts |
| `is_feasible(problem, bp)` | `bool`, stops at the first feasible vector (the agent's scan) |
| `evaluate_offer(client, offer, rules)` | The take-home API, unchanged output |
| `explain.explain(problem, bp)`, `whatif.what_if(problem, change)`, `whatif.unlocking_changes(problem, ask_bp)` | Section 5 |

The agent reaches the engine only through `app/adapter/engine_adapter.py`, which also
scans 1%–100% for the affordability curve. `app/adapter/validator.py` re-checks every
drafted schedule with code that imports nothing from the engine's search or scoring.

## 2. The prefix-cap formulation

Fix the client, the rules and the first payment date. Simulate the ledger once with no
creditor payments, no bank fees and no program fee: the *base* run. A k-payment vector
`p` with prefix sums `P_j = p_1 + .. + p_j` only lowers balances from its payment dates
on. So on every day d in window j (`t_j <= d < t_{j+1}`):

```
balance(d) = base(d) - P_min(j,k) - bf * min(j,k)
```

Two consequences carry the whole design:

- **One simulation per client, not per candidate.** The window minima `w_j` of any
  vector are the base window minima minus a constant. `Problem` holds the base run, its
  window minima and the pre-window minimum; it is built once per affordability scan
  and reused for all 100 prices and for every what-if that does not move the cadence.
- **Every money rule is a cap on a prefix sum.** "No overdraft in window j" is
  `P_j <= base_w[j] - bf*j`, and those caps do not depend on the price. The price only
  sets S, the last prefix sum.

The fee placement has a closed form. Under the take-home's front-loaded rule the
cumulative fee by `t_j` is `Φ_j = min(F, min_{i>=j} w_i)`: take as much as every later
window can spare. A vector is feasible iff the pre-window minimum is at least zero,
every `w_j >= 0`, and `w_M >= F`.

`tests/engine/test_prefix_cap.py` checks the identity against a full ledger replay for
all 285,467 vectors of both search stages on the 1,305 golden-corpus offers (every
balance, every window minimum, the pre-window minimum).

## 3. Exact search and the dominance argument

**Monotonicity.** By section 2, feasibility, fee timing Φ, the slack terms of the
take-home key, the lump L and the increment X depend on a vector only through
`P_1..P_{k-1}`, and pointwise smaller prefix sums never hurt: feasibility is kept, Φ
goes weakly up, L and X weakly down. So it is enough to search a *dominating family*:
a set of legal vectors such that every legal vector of the same length has a member
with pointwise smaller or equal prefix sums. The best key over the family is the best
over every legal vector.

**The family** (`feasibility/search.py`, proofs in its docstring; it ports the
reference solver `tests/engine/reference.py` built in Phase 57):

- *Even rules*: one vector per k.
- *Balloon rules*: the floors-pinned balloon (every payment at its floor, the rest in
  the last) has pointwise-minimal prefix sums, so it dominates every legal vector.
- *Staircases*, per composition of k into m runs of lengths `c_1..c_m` with levels
  `v_1 < .. < v_m`. Shift argument: lowering `v_l` by `D_l = c_m / gcd(c_l, c_m)` and
  raising `v_m` by `c_l*D_l/c_m` keeps the sum, the order, the floors and the run count
  and lowers every prefix sum weakly. So levels in the box
  `v_l in [lo_l, lo_l + D_l)` dominate, where `lo_l` is the larger of the floor at the
  end of run l and `v_{l-1} + 1`. For `l = m-1` the box holds at most one value with the
  right residue, the smallest, and then `v_m` is fixed by the sum.

**Two stages**, so the output changes only where the take-home family was not exact:

1. *Pinned*: the take-home family reduced to the vectors that can win (per composition
   the closed-form smallest `v_{m-1}`, early levels at their lower bounds; the
   quasi-even vector; the floors-pinned balloon). Same winner as the take-home engine.
   This alone is exact for even rules, balloon rules and staircases of at most two
   levels.
2. *Box*: only for plain staircases with `max_segments >= 3`. Early levels range over
   their boxes. A box vector replaces the stage-1 answer only when it is strictly better
   (Φ for Part 1, L or X for Part 2).

**Branch and bound, and why pruning cannot lose the optimum.** A partial vector gives
lower bounds on its later prefix sums (later levels are above the current one and at or
above their floors). The objective bound computed from those lower bounds is valid for
every extension (monotonicity), and it only gets worse as the current level rises, so
a level scan stops at the first failure. The same holds for the remaining-sum test.
Bounds from the floors' prefix sums skip whole payment counts.

**Rescue is exact by the same argument.** L and X are monotone in prefix sums, so `L*`
and `X*` are minima over the same family, with no binary search
(`rescue.exact_lump`, `rescue.exact_increment`). The lump date keeps the take-home rule:
the first date the (L, k, payments)-smallest vector breaks (`rescue.latest_date`).

The take-home engine scored about 7,000 candidates per price on large cases and took up
to 10.8 s for a 100-point scan; the search takes at most 78 ms on the same cases
(section 9).

## 4. Objectives and fee policy

Written before the code (Phase 59). `feasibility/objectives.py` implements these keys as
written, and `tests/engine/oracle.py` ranks every legal vector by keys written from
this section, not from the code.

### Notation

- A plan is k creditor payments `p_1 <= .. <= p_k` on cadence dates `t_1..t_k`. The
  prefix sums are `P_j` (`P_j = S` for j >= k), S is the offer total and F is the
  program fee. Money is integer cents.
- `w_j` is the lowest balance in window j (`t_j <= d < t_{j+1}`) after committed ledger
  entries, creditor payments and bank fees, before the program fee. By the prefix-cap
  identity, `w_j = base_w[j] - P_min(j,k) - bf*min(j,k)`.
- `b` is the soft buffer: the firm's minimum client cushion for this call
  (`Problem.min_cushion_cents`; 0 when none). See "Minimum client cushion" below.
- `Φ_j` is the program fee collected by `t_j`.

### Fee policy (`Problem.fee_policy`)

| Policy | Cumulative fee by `t_j` |
|---|---|
| `front_loaded` (default; take-home) | `Φ_j = min(F, min_{i>=j} w_i - b)` |
| `proportional` | `Φ_j = min(F, min_{i>=j} w_i - b, floor(F * P_j / S))` |

Both are the pointwise-largest fee placement that keeps every balance at or above b.
The proportional cap is non-decreasing in j, so it fits inside the same suffix-min closed
form. A plan is feasible iff the pre-window minimum is >= b, every `w_j >= b`, and
`w_M - b >= F`. This test is the **same under both policies**: at `t_M` the cap is
`floor(F*S/S) = F`. So affordability scans and Part 2 rescue amounts (L\*, X\*) do not
depend on the policy or the objective. The adapter's scan uses that:
`engine.is_feasible(problem, bp)` stops at the first feasible vector and ranks nothing.

The proportional policy is a simple model, not legal advice. It is loosely based on the
FTC Telemarketing Sales Rule advance-fee provision for debt relief services, 16 CFR
310.4(a)(5)(i). That rule bars fees until at least one debt is settled and the customer
has made at least one payment under the settlement, and (C)(1) ties each debt's fee to
that debt's share of the enrolled balance. Our model is per debt and stricter: the fee
collected never runs ahead of the share of this settlement already paid to the creditor.
The validator rule `fee_proportional` checks it when the policy is on.

### Objectives (`Objective`)

Both keys are maximised under Python tuple order.

```
fee_first    = (Φ, -k, min_slack, total_slack, -cliff, -shape_order, -payments)
client_first = (cushion, -p_max, Φ, -k, min_slack, total_slack, -cliff, -shape_order, -payments)

cushion   = min_{1<=j<=M} w_j - F      # lowest balance with the whole fee held back
p_max     = p_k                        # the largest creditor payment
min_slack = min(pre_min, min_j (w_j - Φ_j)),  total_slack = Σ_j (w_j - Φ_j)
cliff     = p_k / lower median of p,   shape_order: even < staircase < balloon
```

`fee_first` is the take-home `scoring.score_key`, unchanged (Φ under the active policy).
`client_first` puts the client's room to breathe first, then the smallest largest payment,
then the firm's fee timing, then fewer payments. Its tail is the fee-first key, so ties
break the same way. `evaluate_offer` (the take-home wrapper) is always `fee_first` +
`front_loaded`. The agent uses `ENGINE_OBJECTIVE` / `ENGINE_FEE_POLICY` (defaults
`fee_first` / `front_loaded`; [DESIGN ADR 7](DESIGN.md#adr-7-how-negotiation-decisions-are-made)
lists the settings). Under `fee_first` + `front_loaded` a post-pass (Tier B) flattens a
large last-payment cliff when that keeps Φ.

**Default: fee-first.** Client-first was the first default. It spreads the price over
more payments, and each payment carries the bank fee. Over the 357 feasible scenario
points it cost the client a median $19.00 (max $38.00) more in bank fees than
fee-first. On the 100-call oracle eval the 21 deals went from an average of 3.0 to 8.3
payments. So the default is the take-home ranking. Client protection comes from the
firm's minimum cushion instead, which is a floor, not a preference for more payments.
`client_first` stays available as a setting, with its tests and the measurements below
as evidence (decision D8).

### Minimum client cushion (soft, per firm, per call)

- **What it is.** `min_cushion_cents`: money the plan should leave in the client's
  account at every point, with fees taken. It is the firm's client-protection policy.
- **Where it comes from.** `firm.json` `min_cushion_cents` (integer cents, 0 to 500,000,
  i.e. up to $5,000; 0 when absent) is the firm default. The operator can change it for
  one call in the Debt negotiator view; it travels in the `start` event
  (`start.min_cushion_cents`), is validated by the server, and is not stored.
- **How the engine uses it.** It is the Tier C buffer `b`, under every objective and fee
  policy. The engine first looks for the best plan that keeps every balance at or above
  `b` (pre-window balances, every window after the fee). When no plan at that price can,
  it falls back to the best plan at `b = 0`. `Result.cushion_kept` says which (`None`
  when the cushion is 0 or nothing is feasible).
- **What it never changes.** Which prices are affordable: the affordability scan
  (`is_feasible`) runs at `b = 0`, so the agent's highest offer does not move. Nor the
  rescue amounts L\* and X\*, or the take-home wrapper `evaluate_offer` (always 0).
- **Who sees it.** PRIVATE. It is in the operator brief, the operator trace ("The plan
  keeps at least $100 in the client's account." / "No plan at this price keeps $100 in
  the client's account, so the agent used one that goes lower."), a private audit row and
  the guard blocklist. Never the rep stream, never NLG input.
- **Check.** Requiring `b` on every balance equals starting the client `b` lower and
  requiring 0, so a kept result must match the cushion-0 result for the shifted client
  payment for payment (`tests/engine/test_cushion.py`, all four modes). That check found a
  latent bug in the cliff post-pass: its slack bound ignored the buffer (fixed; no
  effect at `b = 0`).

Trade-off of `client_first`: more payments mean more bank fees (`bf` each), so a
client-first plan can cost the client a few dollars more in total while keeping more
money in the account at the tightest point. That key ranks the low point first, by design.

### Why cushion holds the whole fee back (measured before fixing the key)

The plan's first idea was "minimum post-fee window slack", `min_j (w_j - Φ_j)`. Under
the front-loaded fee it is degenerate. Φ fills every window up to its suffix minimum, so
at the global minimum window the slack is exactly b. Hence
`min_slack = max(b, min_j w_j - F)` (capped by `pre_min`). It equals b whenever the fee
cannot all be taken at `t_1`. Measured on the fee-first engine, cases with at least two
candidate vectors (scratch script, Phase 59):

| Set | Feasible | ≥ 2 vectors | Slack tied across all vectors | Fee-reserved cushion tied |
|---|---|---|---|---|
| 7 scenarios × 100 bp | 357 | 336 | 42 (12.5%) | 31 (9.2%) |
| 600 golden random | 229 | 214 | 86 (40.2%) | 24 (11.2%) |

On golden, the fee-first winner's slack is exactly b = 0 in 76 of 229 feasible cases. So
cushion is measured with the whole fee held back from the start: `min_j w_j - F`. It
equals the post-fee slack whenever that is above b, and it keeps ranking plans when the
slack is clipped. It does not depend on the fee policy or on b, and it leaves out
`pre_min`, which no plan can change. Remaining ties are mostly plans whose low point is
a window after the last payment, which depends on k only.

### Monotonicity and exactness of the new keys

A key is *monotone* if pointwise smaller prefix sums never make it worse. Feasibility,
cushion, front-loaded Φ, the slack terms, L and X are monotone, so the dominating family
plus branch and bound is exact for them (section 3). Two parts of the new keys are not:

- `-p_max` prefers **larger** prefix sums (`p_k = S - P_{k-1}`). The client-first search
  solves it in three exact stages (proofs in `objectives.py`):
  1. `C*`: the largest cushion, by bisection over C; each test scans the dominating
     family under the caps `P_j <= capw_j - max(F + C, b)`.
  2. `T*`: the smallest largest payment among plans with cushion `C*`. Cushion
     `>= C*` is a set of prefix caps `P_j <= U_j`. "Max payment <= T" is monotone in T,
     so T is searched upward from a lower bound. Each test runs over a family that
     dominates every legal vector **with all payments <= T**. That family is the box
     family with last level <= T, plus vectors whose last level is within `n - c` of T,
     recursively (shift argument with a capped absorbing level).
  3. Among plans with cushion `C*` and max payment `T*`: best Φ, then fewer payments,
     then the fee-first tail, over the same capped family.
- Proportional Φ is not monotone. Smaller prefix sums lift the suffix-min term but
  lower the `floor(F*P_j/S)` cap. Under `proportional` the engine is exact for
  feasibility, cushion, `p_max`, L and X (policy-independent). Φ is maximised over the
  dominating family (fee-first) or the capped family (client-first). It is checked
  against the oracle, which ranks every legal vector. On 15,000 small cases (4,449
  feasible) the engine's Φ is below the all-legal optimum in 147 fee-first and 35
  client-first cases; every other key agrees.

Exactness claims, checked against `tests/engine/oracle.py` (every legal vector, small
cases) and `tests/engine/reference.py` (full size):

| Objective, policy | Exact over every legal vector |
|---|---|
| `fee_first`, `front_loaded` | feasibility, Φ\*, L\*, X\* (Phase 57b) |
| `client_first`, `front_loaded` | feasibility, `C*`, `T*`, Φ\* given both, k given all three |
| either, `proportional` | feasibility, `C*`, `T*` (client-first), L\*, X\* |

Later tie-breaks are deterministic over the search's family, not claimed exact.

## 5. Explain and what-if

**Why a price does not fit** (`feasibility/explain.py`, Phase 60).
`explain(problem, bp)` returns `None` when a plan exists, else the first reason that
applies:

| Reason | Meaning | Shortfall |
|---|---|---|
| `horizon` | No payment date fits before the savings plan ends | none |
| `floors` | The offer total is below the creditor's minimum payment | `f_1 - S` |
| `segments` | No plan fits, but one with one more payment amount would | `L*` |
| `cash_short` | The client's balance would go below zero | `L*` |
| `fee_short` | Balances stay at or above zero, the last window cannot cover the fee | `L*` |

For the last three the shortfall is the exact rescue lump `L*` from the search, the
closest payment count is the k of the (L, k, payments)-smallest vector, and the binding
date is that vector's first break date. Adding the shortfall as a credit on that date
makes that plan feasible; one cent less from the start is infeasible for every k
(checked against the oracle on 400 small cases). Feasibility does not depend on the
objective, the fee policy or the cushion, so neither does the answer. It is PRIVATE:
the operator trace shows a plain line ("At 52% the client's savings run short by $140
on March 31, 2027."), the value joins the leak blocklist, and it never becomes an NLG
fact or reaches the rep stream.

**What-if on terms** (`feasibility/whatif.py`). `what_if(problem, TermChange(field,
value))` answers, lazily and per point, whether each price fits under one changed term,
and its highest affordable price. A change to the minimum payment, the payment count,
the structure or the segment limit keeps the base run and recomputes only `k_max` and
the floors; a new first payment date rebuilds the `Problem`. `unlocking_changes(problem,
ask_bp)` lists the single changes under which the ask fits, in the agent's order (date,
minimum payment, max payments, structure, segments), closest value first. The agent's
term alternatives (`app/agent/term_alts.py`) keep their own ranking rules and now ask
the engine instead of rebuilding rules and rescanning: same choice on all 165 recorded
calls, p50 per term-alt turn 3.2 → 2.1 ms (decision D13).

## 6. Verification

Every behaviour change ships with an oracle or golden test (CLAUDE.md). The checks,
from most independent to most specific:

| Check | What it is | Counts |
|---|---|---|
| Brute-force oracle (`tests/engine/oracle.py`) | Enumerates every vector legal under rules 1–9 on small cases (S ≤ ~400 cents, k ≤ 6, M ≤ 8), with its own ledger replay and fee placement; written from the problem statement, not from the engine | Engine = oracle on feasibility, Φ\*, L\*, X\* on 15,000 small cases (seeds 1 + 2, over 5,000 infeasible; 800 more in the fast suite) |
| Oracle, ranked (`oracle.solve_ranked`) | Ranks every feasible legal vector by each objective under each fee policy | 15,000 cases per mode, 4,449 feasible: 0 mismatches where exactness is claimed (section 4 table); the proportional Φ gap counted |
| Exact reference (`tests/engine/reference.py`) | Exact at full size (k up to 24, thousands of dollars, up to 4 levels) by the dominating family; checked against the oracle on 15,000 small cases (0 disagreements) and its dominance brute-forced on small rules | Engine = reference on the 1,305 golden offers, 20,000 random and 20,000 adversarial cases, and all 130 Phase 57 witnesses |
| Strict minimality | Topping the client up by `L*` (or every draft by `X*`) is feasible, one cent less is not, through a rebuilt `Problem` | 366 amounts at full size (fixtures, scenarios × 100 bp, witnesses) |
| Independent validator (`app/adapter/validator.py`) | 10 binding rules plus `fee_proportional`, with no import of the engine's simulation, shapes or scoring | Every schedule in the sets above; every oracle schedule |
| Golden corpus (`tests/engine/golden/`) | 612 records, 1,305 outputs written by the pre-v2 engine: 5 fixtures, 7 scenarios × 100 bp, 600 seeded random cases | Byte-identical since Phase 55; the exact search changed 0 of them (the take-home was already exact there) |
| Prefix-cap identity | Section 2 | 285,467 vectors |
| Cushion shift | Section 4 | All four modes |
| Agent | The 100-call offline policy eval (oracle NLU, template NLG) | Thresholds PASS on every engine phase. All 100 per-call records match the take-home engine's, so the exact search changed no call; only the short-lived client-first default changed read-backs, and it was reverted (D8). [Frozen Phase 61 run](eval/engine_v2_20261028/summary.md) |

Large sets run under the `slow` marker; the fast suite covers every known witness. The
case generators (`tests/engine/gen_cases.py`) use seeded stdlib `random` only.

## 7. What the take-home missed

The engine was not rewritten on suspicion. The gap was measured first (decisions D3–D5),
with the take-home candidate family frozen as `feasibility/legacy.py` for the comparison.
That file and its report script were removed in Phase 61; the reports are frozen.

- **Small cases** ([report](eval/engine_gap_20261023/summary.md)): 5,000 seeded cases
  against the brute-force oracle, plus 80,000 more on other seeds and a random-cap sweep:
  **0** false infeasible, **0** worse fee timing, **0** rescue over-estimates.
- **Full size** ([report](eval/engine_gap_20261025_full/summary.md),
  [reading](eval/engine_gap_20261025_full/notes.md)): against the exact reference.

  | Set | Cases | False infeasible | Worse Φ\* | Lump over-asked | Increment over-asked |
  |---|---|---|---|---|---|
  | Golden (fixtures, scenarios × 100 bp, 600 random) | 1,305 | 0 | 0 | 0 | 0 |
  | Seeded random | 20,000 | 0 | 0 | 0 | 0 |
  | Adversarial (aimed at the pins) | 20,000 | 2 | 2 | 13 | 8 |

  A structural sweep with no ledger found that 112 of 7,517 tiered rule draws admit
  window caps on which the take-home family misses (all 112 built and verified end to
  end), and **0** of 12,483 tier-free draws (28 M family vectors).
- **Mechanism.** Every miss is a staircase with at least three amounts, min-payment
  tiers and tight cash. The take-home pins the early levels at their lower bounds, which
  can leave the remainder in a residue class the last two runs cannot absorb. Smallest
  witness: k = 5, S = 211 cents (`test_reference.py`). Proved exact: even, balloon,
  staircases of at most two levels.
- **Cost when it misses.** Mostly cents, but up to a $161.78 lump asked when no extra
  money is needed (`a57_07972`), a $44.00 lump where $14.18 is enough, and $11.98 more
  per draft where $0.86 is enough.
- **After the fix** ([re-run](eval/engine_gap_20261027_full/summary.md)): the engine has
  **0** misses on every set, and the witnesses are frozen in
  `tests/engine/golden/witnesses_57.jsonl` and replayed on every test run.

The other two take-home weaknesses (decision D1) were not bugs: its ranking served the
firm only (section 4), and an infeasible price came back as a bare `false` (section 5).

## 8. The affordability curve

[DESIGN ADR 5](DESIGN.md#adr-5-feasibility-is-non-monotonic-so-scan-the-curve) scans all
100 whole percentages instead of binary-searching the highest affordable one. The exact
engine confirms that is still needed: holes in the curve are now proven real, not
artefacts of a partial candidate family. Measured on the Phase 61 engine (scratch
script; a curve is non-monotone when an infeasible point sits between two feasible
ones):

| Set | Curves with a feasible point | Non-monotone | Lowest point infeasible |
|---|---|---|---|
| 7 scenarios (true rules) | 4 of 7 | 0 | 4 |
| 600 golden random cases | 537 | 57 (10.6%) | 352 |
| 300 adversarial cases | 216 | 118 (54.6%) | 211 |

On the scenarios the curve is one band: low prices fail on the creditor's payment
floors, and `balloon_structure` also fails above 77% when cash runs out. Interior holes
come from floors and segment limits that let one total split legally and the next not.
The Phase 57 hunt found no miss on the scenarios, so the agent's curves were already
right before the exact search; the change matters for tiered staircases.

## 9. Benchmarks

Frozen in [`eval/engine_v2_20261028/`](eval/engine_v2_20261028/bench.md) (Apple M5, one
process, 2026-10-28); the Phase 57b before/after and Phase 59 objective runs are in
[`eval/engine_v2_20261027/`](eval/engine_v2_20261027/bench.md). A scan is
`build_problem` once plus 100 prices.

| Measure | Value |
|---|---|
| Agent's scan (`is_feasible`) on the demo | 0.46 ms (take-home engine: 3.61 ms on this machine in Phase 57b; 8.3 ms first measured on a faster machine) |
| Agent's scan, 600 golden random cases, p50 / max | 0.51 / 4.83 ms |
| Ranked fee-first scan, golden p50 / max | 3.12 / 78.3 ms (take-home: 8.2 ms / 10.8 s) |
| One ranked fee-first evaluate (what a spoken plan costs), golden max | 3.35 ms |
| One ranked client-first evaluate, golden max | 66.6 ms |
| Budget: 150 ms per scan on scenarios, demo and golden | Met in every mode for the agent's scan |

## 10. Limits and open issues

- The take-home family's completeness without tiers is a conjecture with strong evidence,
  not a proof. The engine no longer depends on it.
- Under the `proportional` fee policy Φ is exact over the family only (gap in section
  4), and a fee-first + proportional scan can take seconds because Φ cannot be pruned.
  The mode is opt-in.
- Ranked client-first has a slow adversarial tail (one case 3.7 s per 100-point scan;
  14 of 30,000 adversarial evaluates over 150 ms). Many segments (6–8) also grow the
  composition tree past the budget; scenarios use 2–3.
- `segments` in `explain` means one more amount fits; a case that needs two more reports
  `cash_short` / `fee_short`, which is still true.
- The agent never proposes structure or segment changes, though `unlocking_changes` can
  test them.

Current open items across the project are listed at the end of
[`history/DECISIONS.md`](history/DECISIONS.md#open-items).

## Code map

| File | Role |
|---|---|
| `feasibility/models.py`, `util.py`, `constants.py` | Inputs, bp/cent arithmetic, guardrails |
| `feasibility/problem.py`, `simulate.py` | `Problem`, the base run, the prefix-cap identity |
| `feasibility/shapes.py` | Floors, shape labels, static legality |
| `feasibility/search.py` | The dominating family and branch and bound (fee-first, L\*, X\*) |
| `feasibility/objectives.py` | Objective keys, the exact client-first search |
| `feasibility/scoring.py` | Fee placement, the take-home key, the cliff post-pass |
| `feasibility/rescue.py` | Part 2 amounts, dates, guardrail texts |
| `feasibility/engine.py` | Entry points, schedule rows |
| `feasibility/explain.py`, `whatif.py` | Section 5 |
| `tests/engine/` | Oracle, reference, generators, golden corpus, all engine tests |
| `scripts/engine_bench.py` | The benchmark |
