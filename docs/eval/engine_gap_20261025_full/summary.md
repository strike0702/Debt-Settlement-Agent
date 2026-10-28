# Engine gap report, full size: legacy family vs exact reference (2026-10-25)

Command: `uv run python scripts/engine_gap_report.py --mode full --seed 57 --random 20000 --adversarial 20000 --structural 5000 --structural-small 5000 --ref-check 1:5000,2:10000 --prune-check 300` (159.2 s on 8 workers).

Legacy = `feasibility/legacy.py` (frozen take-home family) scored by the engine. Reference = `tests/engine/reference.py` (exact over every vector legal under rules 1–9, by a dominating family and branch and bound; exactness argument in its docstring). Every reference witness is replayed by `oracle.Instance` (independent ledger replay) and checked by `app/adapter/validator.py`; rescue witnesses are feasible at exactly L*/X* and not one cent below. Evidence only; nothing is fixed.

## Reference vs brute-force oracle (small cases)

| seed | cases | oracle feasible | disagreements (feasibility, Φ*, L*, X*) |
|---|---|---|---|
| 1 | 5000 | 1572 | 0 |
| 2 | 10000 | 3024 | 0 |

Pruning self-check (whole dominating family scored, no bounds): 0 disagreements on 900 full-size cases.

## Full-size case sets

| set | cases | ref feasible | legacy feasible | both infeasible | false infeasible | worse Φ* | lump over | increment over | bugs | ref p50 / max s |
|---|---|---|---|---|---|---|---|---|---|---|
| golden | 1305 | 590 | 590 | 715 | 0 | 0 | 0 | 0 | 0 | 0.000 / 0.27 |
| random | 20000 | 7293 | 7293 | 12707 | 0 | 0 | 0 | 0 | 0 | 0.001 / 0.76 |
| adversarial | 20000 | 8354 | 8352 | 11646 | 2 | 2 | 13 | 8 | 0 | 0.008 / 2.75 |

Golden = 5 fixtures + 7 scenarios × 100 bp (true rules) + 600 random (`gen_cases.random_cases(55, 600)`). Random = `gen_cases.random_cases(seed, n)`. Adversarial = `gen_cases.adversarial_cases(seed, n)` (planted lifted levels, shared-factor runs, tiers/tokens inside runs, tight caps, mid-month/EOM). "ref p50 / max s" is reference + legacy + witness checks per case.

## Structural sweep (no ledger)

For random staircase rules (3–4 segments, tiers, tokens) and S near the floors' sum (each draw also repeated with its tiers removed), every reference family vector is tested for a legacy vector of the same k with pointwise smaller or equal prefix sums. No such vector means a ledger exists (caps equal to its prefix sums) where legacy misses; one is built and verified end to end for the smallest non-dominated vector of each draw.

| size | draws | family vectors | draws with a miss | vectors not dominated | witnesses verified (false infeasible) |
|---|---|---|---|---|---|
| small | 5000 | 271721 | 32 | 47 | 32 |
| full | 5000 | 11943059 | 80 | 267 | 80 |
| small, tiers removed | 5000 | 437128 | 0 | 0 | 0 |
| full, tiers removed | 5000 | 23823296 | 0 | 0 | 0 |
| all draws without tiers | 12483 | 28017580 | 0 | 0 | 0 |
| all draws with tiers | 7517 | 8457624 | 112 | 314 | 112 |

## Misses by kind, set and structure

| kind / set / structure | count |
|---|---|
| false_infeasible | adversarial | staircase s=3 | 2 |
| false_infeasible | structural | staircase s=3 | 92 |
| false_infeasible | structural | staircase s=4 | 20 |
| rescue_over_increment | adversarial | staircase s=3 | 8 |
| rescue_over_lump | adversarial | staircase s=3 | 13 |
| worse_phi | adversarial | staircase s=3 | 2 |

| kind / witness run lengths | count |
|---|---|
| false_infeasible | runs [1, 5, 4] | 4 |
| false_infeasible | runs [1, 3, 4] | 4 |
| false_infeasible | runs [1, 2, 2] | 4 |
| false_infeasible | runs [1, 3, 6] | 3 |
| false_infeasible | runs [1, 4, 3] | 3 |
| false_infeasible | runs [2, 3, 4] | 3 |
| false_infeasible | runs [2, 3, 5] | 3 |
| false_infeasible | runs [1, 3, 3] | 2 |
| false_infeasible | runs [2, 5, 3] | 2 |
| false_infeasible | runs [3, 5, 2] | 2 |
| false_infeasible | runs [1, 4, 2] | 2 |
| false_infeasible | runs [4, 5, 12] | 2 |
| false_infeasible | runs [1, 4, 6] | 2 |
| false_infeasible | runs [1, 2, 5] | 1 |
| false_infeasible | runs [1, 2, 7] | 1 |
| false_infeasible | runs [3, 1, 2, 2] | 1 |
| false_infeasible | runs [3, 4, 2] | 1 |
| false_infeasible | runs [1, 3, 4, 2] | 1 |
| false_infeasible | runs [1, 7, 2] | 1 |
| false_infeasible | runs [1, 3, 2] | 1 |
| false_infeasible | runs [6, 2, 5, 9] | 1 |
| false_infeasible | runs [1, 3, 8, 6] | 1 |
| false_infeasible | runs [5, 2, 12] | 1 |
| false_infeasible | runs [7, 3, 4, 2] | 1 |
| false_infeasible | runs [5, 10, 2] | 1 |

| kind / witness has a lifted early level (above the legacy pin) / legacy family empty for this S | count |
|---|---|
| false_infeasible | lifted yes | legacy family not empty | 114 |
| rescue_over_increment | lifted no | legacy family not empty | 1 |
| rescue_over_increment | lifted yes | legacy family not empty | 7 |
| rescue_over_lump | lifted yes | legacy family not empty | 13 |
| worse_phi | lifted yes | legacy family not empty | 2 |

## Run time (s)

| step | seconds |
|---|---|
| reference_vs_oracle | 1.1 |
| golden | 0.6 |
| random | 15.9 |
| adversarial | 65.0 |
| prune_check | 4.3 |
| structural | 63.8 |
| shrink | 6.2 |
| total | 159.2 |

## False infeasible (legacy walks away from a legal deal): smallest shrunk witnesses

- `s57_01877`: S=211, F=0, k_max=5, M=5, staircase, min=6, tokens=5, tiers=[[2, 10], [4, 90]], segments=3, bank_fee=0, first payment 2027-07-15
  - client: balance 0, as of 2027-07-05, credits [('2027-07-15', 7), ('2027-08-15', 10), ('2027-09-15', 10), ('2027-10-15', 92), ('2027-11-15', 92)], debits []
  - legacy: infeasible, lump=3, increment=3
  - reference: [7, 10, 10, 92, 92], Φ*=[0, 0, 0, 0, 0] (runs [1, 2, 2], lifted levels [1])
  - (set structural, shrunk from `s57_01877`)
- `s57_01558`: S=219, F=0, k_max=5, M=5, staircase, min=22, tokens=2, tiers=[[2, 31], [4, 44]], segments=3, bank_fee=0, first payment 2027-09-15
  - client: balance 0, as of 2027-09-05, credits [('2027-09-15', 23), ('2027-10-15', 31), ('2027-11-15', 31), ('2027-12-15', 67), ('2028-01-15', 67)], debits []
  - legacy: infeasible, lump=8, increment=8
  - reference: [23, 31, 31, 67, 67], Φ*=[0, 0, 0, 0, 0] (runs [1, 2, 2], lifted levels [1])
  - (set structural, shrunk from `s57_01558`)
- `s57_02802`: S=234, F=0, k_max=5, M=5, staircase, min=17, tokens=2, tiers=[[2, 25], [4, 71]], segments=3, bank_fee=0, first payment 2027-10-15
  - client: balance 0, as of 2027-10-05, credits [('2027-10-15', 18), ('2027-11-15', 25), ('2027-12-15', 25), ('2028-01-15', 83), ('2028-02-15', 83)], debits []
  - legacy: infeasible, lump=7, increment=7
  - reference: [18, 25, 25, 83, 83], Φ*=[0, 0, 0, 0, 0] (runs [1, 2, 2], lifted levels [1])
  - (set structural, shrunk from `s57_02802`)

## Worse Φ* (legacy collects the fee later than possible): smallest shrunk witnesses

- `a57_10389`: S=90283, F=33100, k_max=10, M=10, staircase, min=5500, tokens=2, tiers=[[5, 5537], [9, 5585]], segments=3, bank_fee=62, first payment 2026-02-28
  - client: balance 0, as of 2026-02-23, credits [('2026-02-26', 19323), ('2026-03-30', 18994), ('2026-05-30', 5564), ('2026-06-27', 5647), ('2026-07-29', 5551), ('2026-08-31', 20733), ('2026-09-29', 6837), ('2026-10-31', 12525), ('2026-11-30', 50348)], debits [('2026-04-30', 21519)]
  - legacy: [5501, 5501, 5501, 5538, 5538, 5538, 5538, 5538, 23045, 23045], Φ=[71, 71, 71, 71, 71, 71, 5859, 5859, 5859, 33100]
  - reference: [5502, 5502, 5502, 5537, 5537, 5537, 5537, 5537, 23046, 23046], Φ*=[71, 71, 71, 71, 71, 71, 5860, 5860, 5860, 33100] (runs [3, 5, 2], lifted levels [1])
  - (set adversarial, shrunk from `a57_10389`)
- `a57_17661`: S=14879, F=468, k_max=11, M=11, staircase, min=751, tokens=11, tiers=[[2, 753], [5, 982]], segments=3, bank_fee=122, first payment 2027-01-31
  - client: balance 0, as of 2026-12-23, credits [('2027-01-30', 878), ('2027-02-27', 878), ('2027-03-31', 1434), ('2027-04-30', 1434), ('2027-05-31', 1434), ('2027-06-28', 1434), ('2027-07-31', 1747), ('2027-08-28', 1747), ('2027-09-30', 1747), ('2027-10-31', 1747), ('2027-11-30', 2221)], debits []
  - legacy: [753, 753, 753, 753, 982, 982, 982, 982, 982, 982, 5975], Φ=[3, 6, 468, 468, 468, 468, 468, 468, 468, 468, 468]
  - reference: [752, 754, 754, 754, 1695, 1695, 1695, 1695, 1695, 1695, 1695], Φ*=[4, 6, 76, 76, 76, 76, 76, 76, 76, 76, 468] (runs [1, 3, 7], lifted levels [1])
  - (set adversarial, shrunk from `a57_17661`)

## Rescue over-estimate: lump sum: smallest shrunk witnesses

- `a57_05096`: S=572, F=1398, k_max=5, M=8, staircase, min=51, tokens=1, tiers=[[2, 53], [4, 206]], segments=3, bank_fee=42, first payment 2026-04-05
  - client: balance 0, as of 2026-03-09, credits [('2026-04-05', 93), ('2026-04-11', 66), ('2026-05-04', 95), ('2026-06-05', 95), ('2026-07-04', 248), ('2026-08-03', 248), ('2026-10-04', 1), ('2026-11-05', 1399)], debits []
  - legacy: infeasible, lump=2, increment=2
  - reference: infeasible, lump=1 via [52, 53, 53, 207, 207], increment=1 via [52, 53, 53, 207, 207] (runs [1, 2, 2], lifted levels [1])
  - (set adversarial, shrunk from `a57_05096`)
- `a57_05227`: S=5547, F=0, k_max=8, M=8, staircase, min=110, tokens=4, tiers=[[2, 114], [6, 165]], segments=3, bank_fee=47, first payment 2027-10-31
  - client: balance 0, as of 2027-10-16, credits [('2027-10-28', 214), ('2027-11-30', 137), ('2027-12-31', 135), ('2028-01-06', 14), ('2028-01-30', 163), ('2028-02-28', 160), ('2028-03-31', 1749), ('2028-04-27', 1672), ('2028-05-30', 1689), ('2028-06-27', 1)], debits [('2028-01-05', 14)]
  - legacy: infeasible, lump=9, increment=3
  - reference: infeasible, lump=8 via [111, 114, 114, 114, 114, 1660, 1660, 1660], increment=3 via [110, 115, 115, 115, 115, 1659, 1659, 1659] (runs [1, 4, 3], lifted levels [1])
  - (set adversarial, shrunk from `a57_05227`)
- `a57_04616`: S=14845, F=0, k_max=8, M=8, staircase, min=1260, tokens=8, tiers=[[2, 1280], [5, 2430]], segments=3, bank_fee=0, first payment 2026-09-28
  - client: balance 109, as of 2026-09-05, credits [('2026-09-28', 1725), ('2026-10-04', 237), ('2026-10-28', 704), ('2026-11-26', 1345), ('2026-12-04', 229), ('2026-12-26', 1215), ('2027-01-28', 2660), ('2027-02-25', 2198), ('2027-03-27', 2430), ('2027-04-25', 2452), ('2027-05-25', 18)], debits [('2026-10-03', 237)]
  - legacy: infeasible, lump=5, increment=2
  - reference: infeasible, lump=3 via [1261, 1280, 1280, 1280, 2436, 2436, 2436, 2436], increment=1 via [1261, 1280, 1280, 1280, 2436, 2436, 2436, 2436] (runs [1, 3, 4], lifted levels [1])
  - (set adversarial, shrunk from `a57_04616`)

## Rescue over-estimate: monthly increment: smallest shrunk witnesses

- `a57_04616`: S=14845, F=0, k_max=8, M=8, staircase, min=1260, tokens=8, tiers=[[2, 1280], [5, 2430]], segments=3, bank_fee=0, first payment 2026-09-28
  - client: balance 109, as of 2026-09-05, credits [('2026-09-28', 1725), ('2026-10-04', 237), ('2026-10-28', 704), ('2026-11-26', 1345), ('2026-12-04', 229), ('2026-12-26', 1215), ('2027-01-28', 2660), ('2027-02-25', 2198), ('2027-03-27', 2430), ('2027-04-25', 2452), ('2027-05-25', 18)], debits [('2026-10-03', 237)]
  - legacy: infeasible, lump=5, increment=2
  - reference: infeasible, lump=3 via [1261, 1280, 1280, 1280, 2436, 2436, 2436, 2436], increment=1 via [1261, 1280, 1280, 1280, 2436, 2436, 2436, 2436] (runs [1, 3, 4], lifted levels [1])
  - (set adversarial, shrunk from `a57_04616`)
- `a57_07176`: S=6431, F=2639, k_max=10, M=11, staircase, min=632, tokens=3, tiers=[[3, 640], [7, 647]], segments=3, bank_fee=166, first payment 2026-11-20
  - client: balance 69, as of 2026-10-15, credits [('2026-11-18', 729), ('2026-12-20', 799), ('2027-01-20', 806), ('2027-02-18', 806), ('2027-03-19', 806), ('2027-04-17', 806), ('2027-05-20', 815), ('2027-06-17', 815), ('2027-07-17', 815), ('2027-08-18', 826), ('2027-09-20', 2638)], debits []
  - legacy: infeasible, lump=15, increment=3
  - reference: infeasible, lump=10 via [635, 640, 640, 640, 640, 640, 649, 649, 649, 649], increment=2 via [633, 633, 640, 640, 640, 649, 649, 649, 649, 649] (runs [1, 5, 4], lifted levels [1])
  - (set adversarial, shrunk from `a57_07176`)
- `a57_00154`: S=104185, F=20452, k_max=10, M=10, staircase, min=8100, tokens=1, tiers=[[3, 8108], [7, 9922]], segments=3, bank_fee=0, first payment 2027-05-31
  - client: balance 6471, as of 2027-04-21, credits [('2027-05-30', 1638), ('2027-06-30', 27584), ('2027-07-31', 7354), ('2027-09-29', 3630), ('2027-10-31', 13927), ('2027-11-30', 15192), ('2027-12-30', 23702), ('2028-01-31', 9287), ('2028-02-29', 23174)], debits [('2027-08-31', 7329)]
  - legacy: infeasible, lump=1187, increment=297
  - reference: infeasible, lump=1182 via [8103, 8103, 8108, 8108, 8108, 12731, 12731, 12731, 12731, 12731], increment=296 via [8103, 8103, 8108, 8108, 8108, 12731, 12731, 12731, 12731, 12731] (runs [2, 3, 5], lifted levels [1])
  - (set adversarial, shrunk from `a57_00154`)
