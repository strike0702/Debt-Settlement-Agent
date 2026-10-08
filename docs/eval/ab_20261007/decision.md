**Decision: A (policy + template NLG) stays the demo default; `Settings.nlg_h3` stays
False.** The adoption rule is applied exactly as pre-registered in REVIEW_PLAN §2(c),
each arm against A. **B fails** on two checks. One is `zero_leaks_and_unverified`: 1
leak in `s0007_006`, which comes from a policy ACCEPT of a sim-invented "100%" and not
from an H3 act. The other is `agreement_valid = 1` (0.71). B passes every other check:
its outcome rates are within A's CIs, its p50 is 1.8 s against A's 2.0 s, and the
judge prefers it, winning 0.80 [0.61, 0.91] of decisive pairs against A. **C fails**
on four checks: 2 leaks and 8 unverified figures spoken in its own text,
`agreement_valid` 0.73, escalation correct 0.22 against A's 1.00, and a p50 of 10.2 s
against 2.0 s. The judge also rates C as more natural than A (0.74 [0.58, 0.85]) and
than B (0.67 [0.50, 0.80]). The naturalness gain does not buy back safety or
escalation. **A also misses `agreement_valid = 1` under live NLU** (0.75). These
failures are live-NLU extraction errors that every arm shares; the oracle CI eval
keeps A at 1.0. The rule is not restated relative to A (user decision 2026-10-07).
H3's naturalness gain is real, so a re-run of B is worth doing once the NLU
extraction errors and the accept-a-sim-invented-figure path are addressed. That is
out of scope for 24b.

**Arm D (LLM-only), partial.** D was dropped at 13/48 on 2026-10-07 (user decision;
the REVIEW_PLAN cut order allows it), so it is left out of the table above, which
would otherwise shrink to n = 13. On the 13 scenarios that A and D share, D is far
behind A:
- 1 leak and 9 unverified figures (A: 0 and 0);
- `agreement_valid` 0.29 against A's 0.86 (n = 7);
- deal rate given ZOPA 0.22 [0.06, 0.55] against 0.78 [0.45, 0.94];
- escalation correct 0.00 against 1.00 (n = 4);
- 1.96 LLM calls per turn and a p50 of 6.9 s, against 0.91 and 1.7 s.

D was not judged for naturalness, and it fails every other check of the rule
(`docs/eval/ab_20261007/partial_D/summary.md`).

**Human check of the judge: pending.** `eval/results/judge_B_vs_A/human_pairs.csv`
holds 20 pairs, sides shuffled, with the key in `human_pairs_key.csv`. It had not
been rated when this summary was written, so the agreement between human ratings
and the judge is not reported.
