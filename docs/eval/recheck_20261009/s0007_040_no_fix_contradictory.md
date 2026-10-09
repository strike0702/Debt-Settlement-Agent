# s0007_040_no_fix_contradictory (run `eval_20261009_062530_s7`)

Audit dump (rep lines, Haiku NLU reading per turn, agent moves, belief corrections). Not an invalid agreement: no deal was drafted. Kept because the sim rewrite slipped the check here; see `summary.md`.

```text
====================================================================================================
s0007_040_no_fix_contradictory haggle easy floor 3500 ask 8400 agreed_bp None final ESCALATE contradiction_unresolved
TRUE: {'max_payments': 3, 'min_payment_cents': 54100, 'payment_structure': 'even', 'first_payment_date': '2026-03-31', 'max_segments': 2, 'max_token_pays': 3, 'min_payment_tiers': ()}
BELIEF final: {'max_payments': (5, 'CONTRADICTED', 2, 'observe'), 'min_payment_cents': (54100, 'KNOWN', 2, 'observe'), 'payment_structure': ('even', 'KNOWN', 2, 'observe'), 'first_payment_date': ('2026-03-31', 'KNOWN', 1, 'observe')}
INTENTS: ['OPENING', 'ASK_SETTLEMENT', 'CLARIFY', 'CLARIFY', 'ESCALATE']
  REP: Sure, we can do up to 3 payments of at least $541 each, even amounts, with the first due March 31. Actually, we can’t.
     NLU: {'turn': 1, 'stance': 'reject', 'settlement_ask_pct': None} [('max_payments', 3, True), ('min_payment_cents', 54100, True), ('payment_structure', 'even', True), ('first_payment_date', '2026-03-31', True)]
   AGENT move: ASK_SETTLEMENT None
  REP: Sorry, that’s not right. Actually, it’s up to 5 payments, minimum $541, even structure.
     NLU: {'turn': 2, 'stance': 'info', 'settlement_ask_pct': None} [('max_payments', 5, True), ('min_payment_cents', 54100, True), ('payment_structure', 'even', True)]
   AGENT move: CLARIFY max_payments
  REP: We can go up to 3 payments. Actually, we can’t.
     NLU: {'turn': 3, 'stance': 'other', 'settlement_ask_pct': None} []
   AGENT move: CLARIFY max_payments
  REP: We can go up to 3 payments. Actually, we can’t.
     NLU: {'turn': 4, 'stance': 'info', 'settlement_ask_pct': None} []
   AGENT move: ESCALATE contradiction_unresolved
```
