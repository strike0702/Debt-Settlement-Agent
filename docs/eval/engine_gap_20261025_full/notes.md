# Engine gap report, full size: reading the result (Phase 57)

`summary.md` is the generated report:

    uv run python scripts/engine_gap_report.py --mode full --seed 57 --structural 5000

It compares the frozen take-home family (`feasibility/legacy.py`) with an exact
reference over every vector legal under rules 1–9 (`tests/engine/reference.py`). Each
reference witness is replayed by `oracle.Instance` (independent ledger code) and passes
`app/adapter/validator.py`. Rescue witnesses are feasible at exactly L\*/X\* and not
feasible one cent below.

## Is the reference right?

- It agrees with the brute-force oracle on feasibility, Φ\*, L\* and X\* on every small
  case of the Phase 55 sets: seed 1 (5,000 cases, 1,572 feasible) and seed 2 (10,000
  cases, 3,024 feasible). 0 disagreements.
- It does not enumerate cents. It enumerates a *dominating family*: every legal vector
  of the same length has a family member with pointwise smaller or equal prefix sums.
  Feasibility, Φ, L and X are all monotone in prefix sums, so the family is enough. The
  argument is in the module docstring: the even vector, the floors-pinned balloon, and
  for staircases the "box" `v_l ∈ [lo_l, lo_l + c_m/gcd(c_l, c_m))` with the last two
  levels in closed form. `test_reference.py` checks it by brute force on small rules:
  every legal vector is dominated and every family vector is legal.
- Pruning is checked against scoring the whole family: 0 disagreements on 900
  full-size cases.
- Speed: per full-size case, reference plus legacy plus witness checks take p50 ≤ 8 ms
  and max 2.75 s. The whole run takes 159 s on 8 workers.

## Result: the take-home family does miss, but only with tiers, ≥ 3 levels and tight cash

| set | cases | false infeasible | worse Φ\* | lump over | increment over |
|---|---|---|---|---|---|
| golden (5 fixtures, 7 scenarios × 100 bp, 600 random) | 1,305 | 0 | 0 | 0 | 0 |
| seeded full-size random | 20,000 | 0 | 0 | 0 | 0 |
| adversarial | 20,000 | 2 | 2 | 13 | 8 |

Structural sweep (no ledger). Each draw is a set of staircase rules plus S near the
floors' sum, and it asks whether every reference family vector is dominated by a legacy
vector of the same k. A non-dominated vector means a ledger exists on which legacy
misses: set each window's cap to that vector's prefix sum. The sweep builds that ledger
for every draw with a miss and runs it end to end. All 112 builds were verified
false-infeasible witnesses.

| draws | family vectors | draws with a miss | vectors not dominated |
|---|---|---|---|
| with tiers: 7,517 | 8.5 M | 112 | 314 |
| without tiers: 12,483 (incl. 10,000 draws repeated with tiers removed) | 28.0 M | **0** | **0** |

How often. Never on the golden corpus or on 20,000 seeded random full-size cases. In
cases built to target the pins, 25 of 20,000 adversarial cases miss (0.13 %), and 112 of
7,517 tiered rule draws admit a cap vector that makes legacy miss.

Which rules. Every miss is a **staircase** (no balloon, no even) with **at least 3
levels** and **min-payment tiers**. 19 of the 25 adversarial miss counts (per kind) also have a
binding token limit (one-off check, not in `summary.md`). Every witness has a lifted early level, i.e. one above the value legacy pins
it to. 92 of the 112 structural misses use `max_segments = 3`, 20 use 4. The smallest
witnesses are k = 5 with runs (1, 2, 2).

How large. Most rescue gaps are 1–10 cents. Some are not:

- `a57_05096`: legacy asks a $44.00 lump; $14.18 is enough.
- `a57_15628`: legacy asks $11.98 more per draft; $0.86 is enough (lump $11.98 vs $8.52).
- `a57_07972`: legacy says infeasible and asks a $161.78 lump. A legal schedule exists
  with no extra money.

The two worse-Φ\* cases differ by one cent of fee on one date, e.g. Φ\* = 847 vs 846 by
the third payment. Under the take-home's *lexicographic* Φ an earlier cent outweighs
anything later. In shrunk witness `a57_17661` the reference's best plan collects 4 cents
on day 1 instead of 3, but only 76 instead of 468 by month 3. That is "better" only by the
fee-first definition, which matters for Phase 59.

## Mechanism: which pin loses it

Legacy fixes levels `v_1..v_{m-2}` at their lower bounds and solves the last two levels
for every residue. The reference proves that the last-two-level solve is complete, so
legacy is exact for even, for balloon (the pinned balloon dominates every legal vector)
and for staircases with ≤ 2 levels. The loss is only the pin on `v_1..v_{m-2}`.

Smallest witness (`s57_01877`, in `test_reference.py`): k = 5, min 6 cents, tokens 5,
tiers from payment 2 at 10 and from payment 4 at 90, 3 segments, S = 211. The floors are
(6, 10, 10, 90, 90). Window cash allows prefix sums 7, 17, 27, 119.

- Reference: (7, 10, 10, 92, 92).
- Legacy's (1, 2, 2) composition pins `v_1 = 6`. That leaves 205 for `2·v_2 + 2·v_3`,
  which is odd, so the composition is dropped.
- Lifting `v_1` by one cent fixes the parity at the cost of one cent earlier.
- Every other composition legacy tries must put more money earlier. The 90-cent tier
  stops the last run from starting before payment 4 at a lower level. Moving boundaries
  costs ≥ 2 cents at some window, so it breaks a cap.

In general, `S − Σ c_l·lo_l` falls in a residue class mod `gcd(c_{m-1}, c_m)` that the
last two runs cannot absorb. The tiers fix the run boundaries and floors near the
levels, so neither another composition nor a 4th level absorbs it within the caps.

## What is proved and what is not

Proved (docstring of `reference.py`, plus brute-force tests):

- The reference is exact.
- Legacy is exact for even, for balloon, and for staircases whose winning composition
  has ≤ 2 levels.
- Legacy is **not** complete in general. The witnesses above are concrete, validated
  counterexamples.

Not proved: legacy is complete when there are no tiers. Floors are then the base
minimum and +1 cent after the token limit. The data supports it strongly: 0
non-dominated vectors among 28 M family vectors in 12,483 draws, plus every random and
golden case. A proof would need an exchange step: for flat floors, show that a vector
with a lifted `v_l` (l ≤ m−2) is dominated by a pinned vector of another composition. The
natural move is to lower `v_l` and shift a run boundary so the sum is kept. It keeps
prefix sums pointwise lower only when some level gap equals the deficit `c_l·d`. I have
not shown that such a gap or a combination of moves always exists. So this is a
conjecture, not a theorem.

## Recommendations

**57b (exact search in the engine): needed, but narrow.**

- Fee-first: the take-home family is exact for even, for balloon, for ≤ 2 levels and,
  by the evidence, for tier-free rules. It misses only with tiers, ≥ 3 levels and tight
  cash. Those cases are rare, but the misses are real and sometimes cost the client real
  money (a $161.78 lump asked when none is needed, a 14× monthly increment). Rules with
  tiers are exactly where real creditors are strictest.
- Port the reference's dominating family (box plus closed form, with branch and bound)
  in place of the staircase pins, and keep everything else. Not a generic search.
- Measure it against the 150 ms per-scan budget. In Python, adversarial worst cases took
  up to about 2.7 s per case including checks, so reusing caps across bp and memoising
  per (k, composition) will matter.
- Phase 59 client-first still needs search beyond this family. "Maximise the minimum
  cushion" is monotone in prefix sums, so the dominating family is enough for it.
  "Minimise the largest creditor payment" is not monotone in prefix sums: the family
  keeps low early sums, which pushes money into the last run. That key needs its own
  bound, or a search over the full box with a different prune.

**58 (exact rescue): merge into 57b.** Legacy over-asks in 21 of 20,000 adversarial
cases (13 lump, 8 increment), plus every false infeasible. L and X are monotone in
prefix sums, so the same dominating family gives exact L\* and X\* directly (minimum over
the family, with branch and bound). The reference already does this, with no binary
search. A separate phase is not needed. 57b should carry the oracle checks (≥ 5,000
infeasible small cases) and the strict-minimality tests that 58 planned.
