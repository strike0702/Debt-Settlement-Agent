# s0007_028_rescue_contradictory (run `eval_20261009_062530_s7`)

Audit dump (rep lines, Haiku NLU reading per turn, agent moves, belief corrections). Not an invalid agreement: no deal was drafted. Kept because the sim rewrite slipped the check here; see `summary.md`.

```text
====================================================================================================
s0007_028_rescue_contradictory haggle easy floor 4400 ask 5600 agreed_bp None final ESCALATE out_of_guardrail
TRUE: {'max_payments': 4, 'min_payment_cents': 23400, 'payment_structure': 'even', 'first_payment_date': '2026-03-31', 'max_segments': 2, 'max_token_pays': 4, 'min_payment_tiers': ()}
BELIEF final: {'max_payments': (6, 'KNOWN', 2, 'ack_corrected'), 'min_payment_cents': (23400, 'KNOWN', 2, 'observe'), 'payment_structure': ('even', 'KNOWN', 2, 'observe'), 'first_payment_date': ('2026-03-31', 'KNOWN', 1, 'observe')}
INTENTS: ['OPENING', 'ASK_SETTLEMENT', 'ASK_SETTLEMENT', 'COUNTER_TERMS', 'COUNTER_TERMS', 'ESCALATE']
  REP: We can take up to 4 payments, minimum $234, even payments, with the first payment due March 31. Actually, we can’t.
     NLU: {'turn': 1, 'stance': 'reject', 'settlement_ask_pct': None} [('max_payments', 4, True), ('min_payment_cents', 23400, True), ('payment_structure', 'even', True), ('first_payment_date', '2026-03-31', True)]
   AGENT move: ASK_SETTLEMENT None
  REP: No, actually it’s a maximum of 6 payments, minimum $234, even structure.
     NLU: {'turn': 2, 'stance': 'info', 'settlement_ask_pct': None} [('max_payments', 6, True), ('min_payment_cents', 23400, True), ('payment_structure', 'even', True)]
     BELIEF ack_corrected {'field': 'max_payments', 'old_value': 4, 'new_value': 6, 'old_status': 'KNOWN', 'new_status': 'KNOWN', 'turn': 2, 'quote': 'a maximum of 6 payments'}
   AGENT move: ASK_SETTLEMENT None
  REP: We need 56% to settle this.
     NLU: {'turn': 3, 'stance': 'counter', 'settlement_ask_pct': 56.0} []
   AGENT move: COUNTER_TERMS alt_first_payment_date
  REP: Sorry, we can’t do that. No.
     NLU: {'turn': 4, 'stance': 'reject', 'settlement_ask_pct': None} []
   AGENT move: COUNTER_TERMS alt_min_payment_cents
  REP: No, that change does not work for us.
     NLU: {'turn': 5, 'stance': 'reject', 'settlement_ask_pct': None} []
   AGENT move: ESCALATE out_of_guardrail
```
