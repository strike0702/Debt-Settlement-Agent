# NLU v2 71a-2 adoption run (2026-11-07): INCOMPLETE

Stopped at the usage limit. Done: scripted calls on Claude Haiku 5.5 (pinned
`providers_haiku.yaml`), both schemas, after the 71a-2 fixes. Not run: the corpus A/B
(main, amounts, held-out stance, held-out negotiation), so criteria 2-5 are not measured.
`NLU_SCHEMA` stays `flat`.

| Criterion 1: scripted calls (19 scripts, 92 turns; 7 new held-out negotiation scripts) | Flat | Acts |
|---|---|---|
| Turns / scripts | 77/92, 14/19 | 91/92, 18/19 |
| NLU p50 (scripted turns) | 1278 ms | 1099 ms |
| Tokens in / out | 194K / 21.6K | 170K / 7.0K |

Both fail `neg_rule_change` #2 (held out): "Actually, I just checked, ... six payments max,
not eight." after our counter → CLARIFY max_payments (8 vs 6), expected COUNTER hold. The
line is read as a plain rule (stance info; `_REVISION_RE` does not match "not eight" /
"actually"), so the orchestrator's post-proposal revision path does not fire. Not fixed:
fixing after seeing a held-out failure would un-hold it. Criterion 1 therefore FAILS for both.
The first acts scripted run crashed (TypeError, tier list value in `_unclaimed_total`) before
finishing; fixed (not a tuning change) and re-run once; the numbers above are that re-run.
Records: `scripted_flat.json`, `scripted_acts.json`. Paid cost: about $0.06 (all Haiku).
