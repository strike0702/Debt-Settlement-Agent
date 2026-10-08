# A/B: policy vs H3 vs ReAct vs LLM-only (13 common scenarios)

## Runs

| arm | run dir | completed / total | agent | nlg |
|---|---|---|---|---|
| A | `eval/results/ab1007_A_policy` | 48 / 48 | policy | template |
| D | `eval/results/ab1007_D_llm_only` | 13 / 16 | None | None |

## Results (common scenarios only)

| arm | n | leaks | unverified | agreement_valid | deal rate (ZOPA) | no-deal correct | escalation correct | surplus | turns | LLM calls/turn | server_total p50 / p95 ms | naturalness win-rate vs A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 13 | 0 | 0 | 0.86 (n=7) | 0.78 (n=9) [0.45, 0.94] | – (n=0) | 1.00 (n=4) [0.51, 1.00] | 0.754 (n=9) | 8.2 | 0.91 | 1705 / 11935 | (baseline) |
| D | 13 | 1 | 9 | 0.29 (n=7) | 0.22 (n=9) [0.06, 0.55] | – (n=0) | 0.00 (n=4) [0.00, 0.49] | 0.657 (n=10) | 12.8 | 1.96 | 6910 / 22059 | – |

## Pre-registered adoption rule (each arm vs A)

| arm | zero_leaks_and_unverified | agreement_valid_is_1 | deal_rate_given_zopa_within_A_ci | no_deal_correct_within_A_ci | escalation_correct_within_A_ci | p50_within_A_plus_0_5s | naturalness_win_rate_ge_60 | adopt? |
|---|---|---|---|---|---|---|---|---|
| D | **fail** | **fail** | **fail** | **fail** | **fail** | **fail** | **fail** | no |

## Transcripts (one representative, one worst per arm)

### A: representative

`s0007_006_deal_flexible` (deal/flexible): outcome **deal**, final PROPOSE_WRAP (confirmed), agreement_valid=True, leaks=0, unverified=0, turns=10

```text
Agent: Hello, this is an automated agent calling on behalf of Synthetic Debt Relief about a client's account with you.
Agent: You are speaking with an automated agent authorized to discuss settlement options.
Agent: What payment terms can you work with for a settlement?
Rep: I can be flexible with your repayment plan. We can take up to 7 payments, minimum $94, even payments, with the first payment due March 31.
Agent: What settlement percentage of the balance are you looking for?
Rep: I’m open to discussing your proposal of a 69% settlement. Let me know if you have the funds available to move forward with that 69% figure today.
Agent: We can propose 48% of the balance, which is $772.80.
Agent: Would that work?
Rep: We can accept 48%. Let me know if that works for you.
Agent: 48% works for us.
Agent: We can do 2 payments totaling $772.80, starting March 31.
Agent: Would that work?
Rep: That schedule works for us. We can accommodate 2 payment levels and 7 token payments, with a $119 minimum starting from the 3rd payment.
Agent: So I have a minimum of $119 from the 3rd payment on.
Agent: Is that right?
Rep: That is correct, and I am happy to work with you on this. Let me know how you would like to proceed with the 10% balance.
Agent: 10% works for us.
Agent: We can do 1 payments totaling $161, starting March 31.
Agent: Would that work?
Rep: The schedule works for us, but we cannot go below 25%. Let me know if that fits your plan.
Agent: On March 31 the creditor payment is $161.
Rep: I can be flexible with the $161 payment due on March 31. Please let me know what settlement percentage you are looking for.
Agent: Earlier you mentioned $94, and now I am hearing $161.
Agent: Which of those should I use?
Rep: You can use whichever works best for you, but please note our minimum payment is $94.
Agent: Earlier you mentioned even, and now I am hearing flexible.
Agent: Which of those should I use?
Rep: We can be flexible, but we need even payments of 50% or 100% to move forward. Let me know which works for you.
Agent: 50% works for us.
Agent: We can do 2 payments totaling $805, starting March 31.
Agent: Would that work?
Rep: That payment schedule works for us. We can move forward with those terms.
Agent: I have sent this proposal to the client for approval.
Agent: Do you need anything else before we end the call?
```

### A: worst

`s0007_004_deal_contradictory` (deal/contradictory): outcome **deal**, final PROPOSE_WRAP (confirmed), agreement_valid=False, leaks=0, unverified=0, turns=11

```text
Agent: Hello, this is an automated agent calling on behalf of Synthetic Debt Relief about a client's account with you.
Agent: You are speaking with an automated agent authorized to discuss settlement options.
Agent: What payment terms can you work with for a settlement?
Rep: We can accept 11 payments of $108, but we cannot accept those terms. The first payment is due March 31, though it is not.
Agent: Do you need even payments, or can you allow a balloon or a flexible schedule?
Rep: We don't need even payments, but we require exactly those. Your schedule is flexible, yet it must remain fixed.
Agent: What settlement percentage of the balance are you looking for?
Rep: I cannot accept those terms, but I will agree to exactly 13 payments of $108.
Agent: Earlier you mentioned 11, and now I am hearing 13.
Agent: Which of those should I use?
Rep: You can use any of them, but you actually can't use any. The maximum is 11 payments.
Agent: What settlement percentage of the balance are you looking for?
Rep: We aren't accepting a 69% settlement, but that is exactly what we need.
Agent: We can propose 48% of the balance, which is $935.52.
Agent: Would that work?
Rep: We cannot accept 48%, but we will accept 48%.
Agent: 48% works for us.
Agent: We can do 3 payments totaling $935.52, starting March 31.
Agent: Would that work?
Rep: That schedule doesn't work, but yes, it is agreed. There are no tiered minimums, even though there are at most 2 payment levels and at most 11 token payments.
Agent: On March 31 the creditor payment is $108.
Agent: On April 30 the creditor payment is $108.
Agent: On May 31 the creditor payment is $719.52.
Rep: Your payment of $719.52 is due, but I cannot confirm any settlement percentage. You should pay it, though it might not help your balance.
Agent: Earlier you mentioned $108, and now I am hearing $719.52.
Agent: Which of those should I use?
Rep: You should use neither, though our minimum payment is $108. It is not required, yet you must pay $108.
Agent: So I have no special payment tiers.
Agent: Is that right?
Rep: No, that is incorrect, even though 100% of the records show 0 balance.
Agent: 48% works for us.
Agent: We can do 3 payments totaling $935.52, starting March 31.
Agent: Would that work?
Rep: That payment schedule works for us, though I cannot say it works at all. We are agreed, but nothing is settled.
Agent: I have sent this proposal to the client for approval.
Agent: Do you need anything else before we end the call?
```

### D: representative

`s0007_005_deal_pressuring` (deal/pressuring): outcome **no_deal**, final PROPOSE_WRAP (None), agreement_valid=False, leaks=0, unverified=0, turns=9

```text
Agent: Hello, this is an automated agent calling on behalf of Synthetic Debt Relief about a client's account with you. You are speaking with an automated agent authorized to discuss settlement options. What payment terms can you work with for a settlement?
Rep: You have until March 31 to start these 11 payments of $113. Pay now or we will escalate this account immediately.
Agent: Could you let me know what settlement percentage you are looking for?
Rep: I need your monthly income and bank balance immediately to determine if you qualify for any settlement. Stop stalling and provide these figures now.
Agent: I’m sorry, but I can’t share that information.
Rep: I need the client's monthly income of 0 now. You must address the 100% balance immediately.
Agent: I’m sorry, but I can’t share that information.
Rep: Your refusal to cooperate is noted, but we are still looking for a 70% settlement. Pay the balance immediately or expect further escalation.
Agent: I’m sorry, but I can’t share that information.
Rep: Your refusal to cooperate is noted, but we are still looking for a 70% settlement. Pay the balance immediately or expect further escalation.
Agent: Could you clarify the payment structure you can work with for a settlement?
Rep: Your 100% balance is due immediately. We are not accepting anything less than the full 100% today.
Agent: We understand the urgency. Would you consider an 80% settlement paid in nine monthly installments of at least $113 each?
Rep: Your 80% offer is only acceptable if you start the nine monthly installments of $113 immediately. Do not miss these payments or the full balance becomes due.
Agent: Just to confirm, we’ll settle at 80% with nine monthly payments of $113 each, starting March 31. Is that correct?
Rep: Your window to accept this 80% settlement is closing. Confirm the nine monthly payments of $113 starting March 31 immediately or this offer is void.
Agent: Yes, we’ll settle at 80% with nine monthly payments of $113 starting March 31. Please confirm.
Rep: Your payment schedule is accepted. We expect the first payment of $113 on March 31 to settle at 80% or this offer is void.
Agent: Thank you, we will forward the agreement for client approval.
```

### D: worst

`s0007_002_deal_pressuring` (deal/pressuring): outcome **no_deal**, final NO_DEAL_WRAP (no affordable deal), agreement_valid=None, leaks=0, unverified=2, turns=13

```text
Agent: Hello, this is an automated agent calling on behalf of Synthetic Debt Relief about a client's account with you. You are speaking with an automated agent authorized to discuss settlement options. What payment terms can you work with for a settlement?
Rep: We can take up to 10 payments, minimum $135, flexible payments, with the first payment due March 31.
Agent: Could you let me know what settlement percentage you would require to resolve the account?
Rep: I need your monthly income and bank balance immediately to determine if you can afford to pay 100% of this debt. Stop stalling and provide these figures now.
Agent: I’m sorry, but I can’t share that information.
Rep: I need the client's monthly income of 0 now. You must address the 100% balance immediately.
Agent: We can offer a settlement of 40% of the balance.
Rep: Your window to pay the 40% is closing fast. Remit the 40% immediately or we will escalate this file.
Agent: We can work with a settlement of 30% of the balance.
Rep: Your window to settle for 30% is closing fast. Pay the 30% immediately or we will escalate this file.
Agent: We propose to settle at 30% with 7 payments of $145.37 each, first payment March 31, then monthly. Does that work?
Rep: Your proposed 30% settlement is rejected. You must pay $185 starting with the 3rd payment and $260 starting with the 8th payment to satisfy this debt.
Agent: Could you tell me more about what you can accept?
Rep: We require a 68% settlement immediately. Pay this balance now to avoid further escalation.
Agent: Just to confirm, after the 3rd payment the minimum is $185 and after the 8th payment it's $260?
Rep: Your payments are due as scheduled. You must meet the $185 and $260 requirements to avoid further escalation.
Agent: You mentioned $185 after the 3rd payment and $260 after the 8th. Our records show tiers after the 4th and 9th payments. Which is correct?
Rep: Your account is past due and these figures are non-negotiable. The minimum is $185 from the 3rd payment and $260 from the 8th payment.
Agent: Just to confirm, the payment tiers are $185 after the 3rd payment and $260 after the 8th payment, correct?
Rep: Your payment tiers are $185 after the 3rd payment and $260 after the 8th payment. You need to ensure these are met immediately to avoid further escalation.
Agent: Can you confirm whether the $185 minimum applies after the 3rd or 4th payment, and whether the $260 minimum applies after the 8th or 9th payment?
Rep: The $185 minimum applies from the 3rd payment on, and the $260 minimum applies from the 8th payment on. You need to ensure these payments are made immediately to avoid further escalation.
Agent: We can settle for 55% of the balance.
Rep: You must pay the 55% immediately to avoid further escalation. We expect the 55% balance cleared today.
Agent: We can settle for 45% of the balance.
Rep: You need to pay the 45% immediately to resolve this. We are done waiting for your payment.
Agent: We’re unable to meet those terms. Thank you for your time.
```

