# Orchestrator runbook: phases 20–25 with Herdr + git worktrees

You are the **orchestrator** for phases 20–25 of `docs/REVIEW_PLAN.md`. You do not implement phases yourself. You start one worker coding agent per phase, each in its own git worktree, through the `herdr` CLI. You give each worker its phase prompt, watch it, check its work, merge it into `main`, and start the next phases in the order the plan sets.

Read `docs/REVIEW_PLAN.md` §3–§5 now: the phase table, the execution strategy, and the phase prompts. Do not read any other part unless a worker's question needs it.

---

## 0. Preconditions (stop if any fails)

1. Check that you are running inside Herdr: `test "${HERDR_ENV:-}" = 1`. If not, say so and stop.
2. Run `herdr --skill` once and follow its rules:
   - Parse IDs from the JSON responses.
   - Use `--no-focus`.
   - Never close panes or workspaces you did not create.
   - Never run `herdr server stop`.
   - Never pass `--trust-repository` unless the user approves.
3. Check that `herdr status` works and `git -C <repo> status --short` is clean, apart from the two docs below.
4. If `docs/REVIEW_PLAN.md` or `docs/ORCHESTRATE.md` is untracked or modified, commit only those two files on `main` as `docs: review plan and orchestration runbook`. Worktrees branch from `main`, so the workers need these files.
5. Tools needed: `uv`, `git`, and `node` + `npm` (for P23a/P23b). Report any that are missing before starting a phase that needs them.
6. Run `claude --help` and confirm `--permission-mode` accepts `auto`. Workers start with `-- --permission-mode auto`. If `auto` is not accepted, use `acceptEdits` and expect more `blocked` states.

## 1. State file (so you can resume)

Keep progress in `../dsa-orchestration.md`, a sibling of the repo, outside git. Update it after every state change. It holds:
- one row per phase with: status (`pending|running|verifying|merged|failed|needs-user`), branch, worktree path, Herdr workspace id, pane id, agent name, start time, merge commit;
- a **carry-over backlog**: one row per deferred item with source phase, location, issue, severity, destination (phase / P26 / user / won't fix), and outcome;
- an append-only event log.

On startup, if this file exists, read it, reconcile it with `herdr agent list`, `git worktree list`, and `git branch`, then continue from where it left off. Never restart a phase that is already merged.

## 2. Phase graph and scheduling rules

| Phase | Branch | Worktree path | Starts when merged into `main` | Allowed paths (scope check) |
|---|---|---|---|---|
| P20 | `phase-20` | `../dsa-p20` | — | `app/`, `tests/`, `eval/run_eval.py`, `README.md`, `docs/`, `.env.example` |
| P21 | `phase-21` | `../dsa-p21` | P20 | `app/`, `config/`, `scripts/`, `tests/`, `docs/`, `README.md`, `eval/nlu_corpus.py`, `.env.example`, `render.yaml` |
| P23a | `phase-23a` | `../dsa-p23a` | P20 | `web/` **only** |
| P24a | `phase-24a` | `../dsa-p24a` | P20 | `eval/`, `tests/` (new files only, except `eval/run_eval.py`) |
| P22 | `phase-22` | `../dsa-p22` | P21 | `app/`, `tests/`, `.github/workflows/ci.yml`, `docs/`, `web/src/types/events.schema.json` |
| P23b | `phase-23b` | `../dsa-p23b` | P22 **and** P23a | `web/`, `app/`, `tests/`, `render.yaml`, `.github/`, `docs/` |
| P24b | `phase-24b` | `../dsa-p24b` | P21 **and** P22 **and** P24a | `app/`, `config/`, `eval/`, `tests/`, `docs/` |
| P26 | `phase-26` | `../dsa-p26` | P23b **and** P24b | the files named by its carry-over items (§5.7), plus `tests/` and `docs/` |
| P25 | `phase-25` | `../dsa-p25` | P26 (or P23b **and** P24b when the P26 backlog is empty) | `README.md`, `docs/`, `.github/`, `app/main.py`, `tests/`, deleted `.gitkeep` files |

P26 is the **carry-over cleanup** phase. It is not in `REVIEW_PLAN.md`; its prompt is in §5.7. It runs before P25 so the README is written against code that has the carry-over fixes. It is skipped when the backlog is empty.

Scheduling rules:
- At most **3 workers** at a time.
- A phase starts only when every phase in its "starts when merged" column is merged, so each worktree branches from an up-to-date `main` with `--base main`.
- Priority when slots free up: P20 > P21 > P22 > P23b > P24b > P23a > P24a > P26 > P25.
- Expected waves:
  1. P20
  2. P21 ∥ P23a ∥ P24a
  3. P22
  4. P23b ∥ P24b
  5. P26 (if there is a backlog)
  6. P25
- If P24b is stuck on free-tier quota for more than 2 hours, set it to `needs-user` and ask the user whether to start P25 without A/B numbers. The plan allows that; P25 then writes "A/B in progress".

## 3. Starting a worker for phase N

1. **Create the worktree workspace:**
   `herdr worktree create --cwd <repo> --branch phase-N --base main --path ../dsa-pN --label "PN <short name>" --no-focus`
   - Read the workspace id from the JSON response.
   - Find its root pane with `herdr pane list --workspace <id>`.
   - Never guess IDs.
2. **Prepare the environment** in that pane:
   - `herdr pane run <pane> "uv sync --group dev"`, then `herdr pane wait-output <pane> --regex "(Resolved|Installed|Audited|error)" --timeout 300000`.
   - Copy the main checkout's `.env` into the worktree (`cp <repo>/.env ../dsa-pN/.env`). It is gitignored; workers need keys for the live steps in P21, P24a, and P24b.
   - Wait until the pane is back at its shell prompt.
3. **Start the agent:**
   `herdr agent start pN --kind claude --pane <pane> --timeout 60000 -- --permission-mode auto`
   - Agent names are lowercase: `p20`, `p21`, `p23a`, ….
4. **Send the prompt** with `herdr agent prompt pN "<text>"` (no `--wait`; you poll in §4). The text is:
   - the **worktree preamble** below,
   - then the phase's prompt copied **verbatim** from `docs/REVIEW_PLAN.md` §5 (for P26, the prompt in §5.7),
   - then, if the backlog has items routed to this phase (§5.7), a final block headed `Carry-over tasks from earlier phases (in scope for you; fix each with a test and list it under "Carry-over" in your PROGRESS handoff):` with one line per item.

Worktree preamble (replace the placeholders):
```
You are running in a git worktree at ../dsa-pN on branch phase-N, created from an
up-to-date main. Overrides to the phase prompt below:
- Do all work and the final commit on branch phase-N here. Do NOT merge, rebase, push,
  or touch other branches or worktrees; the orchestrator merges.
- Any instruction to "merge phase-X first" is already satisfied: main contained it when
  this branch was created. Skip that step.
- Stay inside your phase scope. If you need a file outside it, stop and ask in your reply.
- Anything you find but do not fix (out of scope, "Observed, not fixed", open issues)
  must also be listed in your final message, one per line, in this exact format:
  DEFERRED: <path:line or area> | <what is wrong> | <High|Med|Low> | <suggested fix>
  Write "DEFERRED: none" if there are none. Keep the PROGRESS notes too.
- When finished, end your final message with exactly one line:
  PHASE N DONE <commit-sha>
  or, if you cannot finish:
  PHASE N BLOCKED: <one-line reason>
```

## 4. Monitoring loop

Repeat until every phase is `merged` (or `needs-user`):

1. `herdr agent list`. For each running worker, call `herdr agent wait pN --timeout 300000` one worker at a time. Never block forever on one worker while the others need attention.
2. Act on the worker's state:
   - **`working`**: leave it alone.
   - **`blocked`** (an approval or question dialog):
     - Read it with `herdr agent read pN --source recent-unwrapped --lines 60`.
     - **Approve** only if it is a routine in-scope action inside the worktree: file edits, `uv run …`, `npm …`, `git add`/`commit` on its own branch, reading files.
     - **Deny** with `esc`, and tell the worker why, if it is: anything outside the worktree, `git push`, branch deletion, `rm -rf` beyond build artefacts, installing global tools, or network calls other than the LLM providers and package registries.
     - If in doubt, `herdr notification show "PN needs you" --body "<question>" --sound request`, set `needs-user`, and keep running the other phases.
   - **`idle` / `done`**: read the last 80 lines.
     - If it contains `PHASE N DONE <sha>`:
       - Copy every `DEFERRED:` line into the state file's backlog, tagged with the source phase. Read more lines if the list is longer than 80.
       - If there is no `DEFERRED:` line at all, ask the worker for its list before verifying.
       - Then go to §5.
     - If it contains `PHASE N BLOCKED`, or asks a question:
       - answer it from `docs/REVIEW_PLAN.md` when the plan clearly decides it;
       - otherwise notify the user and set `needs-user`.
     - If it stopped without either marker, ask: "Continue the phase; finish with the PHASE N DONE/BLOCKED line." Do this at most twice, then escalate to the user.
3. Log every action in the state file. Keep your own context small: read at most 80 lines per check, and never paste whole files into prompts.

## 5. Verifying and merging phase N

Run these in a pane you create for checks (a sibling of your own pane, split `--no-focus`), not in the worker's pane:

1. **Commit check:** `git -C ../dsa-pN log main..phase-N --oneline`.
   - At least one commit message must start with `phase N`. For split phases the message is `phase 23a:` / `phase 24b:`.
   - The working tree must be clean.
2. **Scope check:** `git diff --stat main...phase-N`. Every changed path must match the phase's allowed paths in §2. If not, send the worker the out-of-scope paths and ask it to revert them, then repeat this step.
3. **Gates, in the worktree:**
   - `uv run ruff check .`
   - `uv run pytest -q`
   - `uv run python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template --scenarios 100 --seed 7` (must print a threshold PASS; exit 0)
   - P23a/P23b also run `cd web && npm ci && npm run typecheck && npm test && npm run build`.
   - If a gate fails, send the failing command and the last 40 lines of output to the worker: "Fix this, re-run all gates, and commit." Then return to §4.
4. **Phase acceptance:** read the PROGRESS handoff (P23a: `web/README.md`; P24a: `eval/agents/README.md`). Check the phase's specific acceptance items from its prompt, for example:
   - P21: the latency report exists with BEFORE/AFTER;
   - P22: the rep-stream privacy test exists;
   - P24b: `docs/eval/ab_*/summary.md` exists with the adoption decision.

   If an item is missing, send it back to the worker.
5. **Merge, in the main checkout:**
   - `git merge --no-ff phase-N -m "merge phase-N"`.
   - If `docs/PROGRESS.md` conflicts because parallel phases appended sections, keep **both** sections in phase-number order. Resolve any other conflict only if it is mechanical. Otherwise abort the merge, ask the worker to rebase onto `main` in its worktree and re-run the gates, then retry.
   - Re-run the three Python gates on `main` after the merge. If they fail on `main`, `git reset --hard ORIG_HEAD` on main is allowed **only** for the merge you just made. Then return to the worker with the failure.
6. **Clean up:**
   - Record the merge sha.
   - Ask the worker to exit (`herdr agent send-keys pN ctrl+c` twice if needed).
   - `herdr worktree remove --workspace <its id>`, then `git branch -d phase-N`.
   - Notify: `herdr notification show "PN merged" --sound done`.
   - Run the carry-over triage (§5.7), **then** the scheduler (§2) to start newly unblocked phases, so new carry-over items reach phases that have not started yet.

### 5.7 Carry-over triage (after every merge)

**Collect.** Gather the merged phase's `DEFERRED:` lines. Also read the "Observed, not fixed", "Open", and "Still open" lines in its new `docs/PROGRESS.md` section (or `web/README.md` / `eval/agents/README.md`) and add any not already in the backlog. Remove duplicates against existing backlog items and against REVIEW_PLAN findings F1–F19.

**Route each item to exactly one destination**, recorded in the state file:
1. **A later phase that has not started**, if the item's files are inside that phase's allowed paths (§2) and fit its theme. Examples:
   - an NLG copy issue goes to P24b;
   - a WS event issue goes to P22;
   - a UI glitch goes to P23b;
   - a stale README claim goes to P25.

   It is added to that phase's prompt as a carry-over task (§3 step 4).
2. **P26 (carry-over cleanup)**, if no unstarted phase fits, or the phase that would fit is already running or merged.
3. **User**, if fixing it would change a decision in `REVIEW_PLAN.md` §2, change an eval threshold, alter policy behaviour (new or changed reason codes, cascade order), or cost money. Notify with `herdr notification show "Carry-over needs a decision" --body "<item>" --sound request` and list it in the final report.
4. **Won't fix**, only for items already fixed on `main` (check with `git grep` or a test) or exact duplicates. Record the reason.

Never send a carry-over item to a phase that is already running. Mid-phase scope changes cause scope-check failures; use P26 instead.

**P26 prompt** (fill in the item list; start it as in §3 with the same preamble):
```
You are implementing Phase 26 (carry-over cleanup) for Debt-Settlement-Agent.
Read docs/PROGRESS.md and CLAUDE.md. These items were found by earlier phases and
deferred because they were out of their scope:
<one line per backlog item: source phase | path:line | issue | severity | suggested fix>

For each item, tests first:
- Reproduce it (failing test, or a command showing the problem). If it no longer
  reproduces on this branch, mark it "already fixed" with the evidence.
- Fix High and Med items. Fix Low items when the fix is under ~30 lines; otherwise
  mark them "deferred to user" with a one-line reason.
- Do not change policy decisions, reason codes, eval thresholds, or any decision in
  docs/REVIEW_PLAN.md §2. If an item needs that, mark it "needs user decision".
Acceptance: pytest -q, ruff check ., and the oracle eval
(python -m eval.run_eval --nlu oracle --nlg template --sim-phrasing template
--scenarios 100 --seed 7) all green; PROGRESS "Phase 26 (carry-over)" handoff with
a table: item | outcome (fixed / already fixed / deferred to user / needs user
decision) | test or evidence; commit `phase 26: carry-over cleanup`.
```

**P26 checks.** During §5 verification for P26, check that every backlog item routed to P26 appears in its PROGRESS table with an outcome. Any item missing from the table goes back to the worker.

**P25 findings.** Items deferred by P25 itself go straight to the final report; there is no second cleanup pass, so the loop ends.

## 6. Points where you must stop and ask the user

- Before any `git push`. P25's prompt ends with "push": let the worker commit, merge it yourself, then ask the user before pushing `main`.
- Before turning on the keep-warm workflow against the live Render URL, or changing Render settings.
- When a phase needs the human's 20 voice turns (latency report, P21/P25). Notify, keep the other phases going, and accept the report without voice numbers if the user says so.
- Any paid-tier switch, quota top-up, or new API key.
- Any request to change an eval threshold. The answer is always no without the user.
- Any change to the decisions in `docs/REVIEW_PLAN.md` §2.

## 7. Finish

When P25 is merged:
- `uv run pytest -q`, `uv run ruff check .`, and the oracle eval, all on `main`.
- `git worktree list` shows only the main checkout.
- `herdr agent list` shows no workers you started.
- Report to the user:
  - one line per phase with its merge sha;
  - anything set to `needs-user` or skipped;
  - the headline numbers (latency before/after, A/B decision);
  - the outstanding human steps;
  - the **carry-over ledger**: every backlog item with its source phase, its destination, and its outcome (fixed in phase X, with the test name / already fixed / deferred to user / won't fix + reason). No item may be missing from the ledger.

  Then ask whether to push `main`.
