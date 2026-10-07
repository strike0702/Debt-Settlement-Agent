# Note (added 2026-10-07, Phase 25)

These transcripts were frozen on 2026-10-06, before Phase 20 (F13) changed the
agent's opening line. They open with the old three-line greeting ("Good
morning, thank you for calling ..."). The current opening is one line: "Hello,
this is an automated agent calling on behalf of {firm_name} about a client's
account with you. ..." (`app/agent/nlg.py`, `Intent.OPENING`).

The pack is kept as evidence and not regenerated, so `summary.md`, `run.json`
and the transcripts stay from one run (`eval_20261006_004050_s7`). Every later
oracle eval (Phases 20–26) reproduced the same metrics table byte for byte; only
the opening wording differs.
