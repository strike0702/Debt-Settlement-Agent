# Feasibility engine v2

Draft started in Phase 59 with the objectives and fee policy. Phase 61 turns it into the
full document for outside readers: problem, prefix-cap formulation, exact search,
explanations, verification and benchmarks.

## Objectives and fee policy (Phase 59)

Written before the code. `feasibility/objectives.py` implements these keys as written.

### Notation

- A plan is k creditor payments `p_1 <= .. <= p_k` on cadence dates `t_1..t_k`. The
  prefix sums are `P_j` (`P_j = S` for j >= k), S is the offer total and F is the
  program fee. Money is integer cents.
- `w_j` is the lowest balance in window j (`t_j <= d < t_{j+1}`) after committed ledger
  entries, creditor payments and bank fees, before the program fee. By the prefix-cap
  identity (`feasibility/problem.py`), `w_j = base_w[j] - P_min(j,k) - bf*min(j,k)`.
- `b` is the soft buffer (`constants.py`; currently 0).
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
`client_first` / `front_loaded`).

Trade-off: more payments mean more bank fees (`bf` each), so a client-first plan can cost
the client a few dollars more in total while keeping more money in the account at the
tightest point. The key ranks the low point first, by design.

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

### Monotonicity and exactness

A key is *monotone* if pointwise smaller prefix sums never make it worse. Feasibility,
cushion, front-loaded Φ, the slack terms, L and X are monotone, so a dominating family
plus branch and bound is exact for them (`search.py`). Two parts of the new keys are
not:

- `-p_max` prefers **larger** prefix sums (`p_k = S - P_{k-1}`). The client-first search
  solves it in three exact stages (proofs in `objectives.py`):
  1. `C*`: the largest cushion, by branch and bound over the dominating family.
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
