# Parley phases

Run one phase per chat, in order, from the `~/projects/parley` workspace. Paste the prompt as is. The always-on rule `.cursor/rules/parley.mdc` makes each chat read `docs/PROGRESS.md` first and update it at the end. This file replaces the build order in PLAN.md section 12 with finer phases.

If a phase fails its acceptance check, fix it in the same chat before moving on. If a chat runs out of room mid-phase, start a new chat with: "Continue phase N. Read docs/PROGRESS.md and `git status` to see what is done."

---

## Phase 0: scaffold and vendored engine

```
Phase 0 of docs/PHASES.md. Read docs/PLAN.md sections 2 and 3.
1. git init if needed. Create pyproject.toml (Python 3.12, deps from section 2), a uv venv with Python 3.12, .gitignore (venv, *.db, eval/results/, llm_cache.db, .env), and .env.example listing every key from section 3.
2. Vendor /Users/shbagchi/projects/retape_ai_takehome/feasibility/ as a top-level feasibility/ package, unchanged. Copy its tests/test_units.py and tests/test_rescue.py into tests/engine/. Any test that loads cases/ must be re-pointed to our own fixtures in fixtures/engine/ (new synthetic JSON in the same shape, with different numbers) or removed. Do not copy ASSIGNMENT.md or cases/.
3. Create the empty package layout from section 2 (app/..., sim/, eval/, tests/unit, tests/e2e, config/) with __init__.py files, and a pytest config with asyncio mode auto.
4. Create app/config.py with the Settings from section 3 (pydantic-settings, reads .env).
Acceptance: `uv run pytest -q` green on tests/engine, `uv run ruff check .` clean. Then update docs/PROGRESS.md and commit.
```

## Phase 1: domain model

```
Phase 1 of docs/PHASES.md. Read docs/PLAN.md section 4 only.
Implement app/domain/money.py, fields.py, facts.py, belief.py, scenario.py exactly as specified. scenario.py loads a CallScenario (engine Client, public offer fields: creditor, creditor_balance_cents, original_balance_cents; firm: program_fee_pct, bank_fee_cents) from a folder with client.json, offer.json, firm.json. Create fixtures/demo/ with synthetic client.json, offer.json, firm.json.
Tests in tests/unit/: money render/parse round-trips (whole and fractional dollars, bp with decimals, dates with and without year), field registry defaults, Fact visibility, and a table-driven test covering every belief transition in section 4.4 including confirm_readback yes and no.
Acceptance: pytest and ruff green. Record the public signatures in docs/PROGRESS.md Interfaces. Commit.
```

## Phase 2: engine adapter and validator

```
Phase 2 of docs/PHASES.md. Read docs/PLAN.md section 5, and docs/PROGRESS.md Interfaces for the domain types.
Implement app/adapter/engine_adapter.py (build_rules with NeedsInfo, evaluate returning EvalSummary with a FactSet whose visibility follows section 4.3, affordability scan over range(100, 10001, 100) with lru_cache) and app/adapter/validator.py, written independently of feasibility internals (do not import feasibility.simulate, shapes, or scoring; models.py date helpers are fine).
Tests: build_rules raises NeedsInfo for missing, tentative, and contradicted fields; affordability on a fixture with a non-monotonic curve (build one where the curve has a gap, and assert the whole curve); max_bp is None when nothing is feasible; the validator has one failing test per binding rule (10 rules) and passes every feasible engine result on fixtures/engine and fixtures/demo; a PRIVATE/PUBLIC check on EvalSummary facts (balances and fees PRIVATE).
Acceptance: pytest and ruff green. Update PROGRESS.md (Interfaces, plus the measured affordability time on the demo fixture). Commit.
```

## Phase 3: numbers and guards

```
Phase 3 of docs/PHASES.md. Read docs/PLAN.md section 6.4 and the Fact/FactSet interfaces in docs/PROGRESS.md.
Test first: write tests/unit/guard_adversarial.jsonl (at least 40 lines: {"text", "stage": "template"|"rendered", "expect": "block"|"pass", "reason"}) covering spelled-out numbers, "2.5k"-style hidden figures, private values in every format ($2,500 / 2500 dollars / 2,500.00), percentages, dates, ordinals, commitment phrasings, "one" allowlist phrases, and clean rendered sentences that must pass. Write tests/unit/test_guard_regression.py that runs it. Confirm it fails.
Then implement app/agent/numbers.py (token extraction in the section 6.4 order with span masking, normalization to (kind, value), words-to-digits for common forms like "two hundred fifty" and "twenty-five hundred") and app/agent/guards.py (template_guard, rendered_guard returning a GuardResult with ok, reason, offending tokens). Add unit tests for numbers.py.
Acceptance: the regression suite and all tests pass, ruff clean. Update PROGRESS.md and commit.
```

## Phase 4: policy, NLG templates, audit

```
Phase 4 of docs/PHASES.md. Read docs/PLAN.md sections 6.2, 6.3 (template part only), and 9, plus docs/PROGRESS.md Interfaces.
1. app/store/audit.py: the events table, WAL mode, append-only triggers, append and for_call.
2. app/agent/policy.py: NegotiationState, Action, Effect, Agreement, decide(), next_counter(), draft_agreement() exactly as in 6.2. Pure and synchronous: it takes the belief state, the TurnAnalysis (define the Pydantic model from 6.1 in app/agent/nlu_types.py now so policy does not depend on the LLM), and the Affordability result.
3. app/agent/nlg.py: the Intent enum and one deterministic template per intent, plus render_action(action, ref_date) -> list[str] sentences, which runs template_guard and rendered_guard and falls back to SAFE_FALLBACK. No LLM in this phase.
Tests: audit UPDATE/DELETE raise; table-driven policy tests, one per numbered rule in 6.2 (escalation on repeats, contradiction before read-back before ask, counters snap to feasible bps, never at or above the ask, never decreasing, NO_DEAL after MAX_COUNTERS, rescue escalates without speaking amounts); every intent's template passes both guards with sample facts.
Acceptance: pytest and ruff green. Update PROGRESS.md and commit.
```

## Phase 5: LLM client and provider pool

```
Phase 5 of docs/PHASES.md. Read docs/PLAN.md sections 7 and 7a.
Implement config/providers.yaml (all providers and the demo, eval, local, offline profiles) and app/llm/client.py: role-based chat_json, chat_text, and transcribe; a per-provider token bucket plus min_interval_s; failover rules exactly as in section 7 (short retry-after waits, long ones mark the target exhausted, 5xx backoff, Gemini 400 quota mapped to 429, LLMUnavailable when everything is exhausted); skip providers with no key or a failed Ollama health check; json_mode handling with code-fence stripping; the SQLite response cache; the on_call hook; and FakeLLM.
Tests use a fake HTTP transport (httpx.MockTransport passed to AsyncOpenAI via http_client) for: the limiter, 429 short wait, 429 long leading to failover, the Gemini 400 mapping, a cache hit on the second identical call, skipping a missing key, and LLMUnavailable.
Then run a live smoke script, scripts/smoke_llm.py, that sends one tiny nlu-role JSON call per available provider and prints provider, model, and latency. Run it for the providers whose keys are in .env and for Ollama if it is running. Record the results in PROGRESS.md.
Acceptance: unit tests green offline; the smoke script works for at least Groq. Commit.
```

## Phase 6: NLU and LLM NLG

```
Phase 6 of docs/PHASES.md. Read docs/PLAN.md sections 6.1 and 6.3, plus docs/PROGRESS.md Interfaces (TurnAnalysis, numbers.py, guards, nlg templates, LLM client).
1. app/llm/prompts.py: the NLU prompt (field definitions with units, the agent's last line, the utterance, JSON only) and the NLG prompt (placeholders with meanings, never values or digits). Keep both short.
2. app/agent/nlu.py: analyze(utterance, last_agent_line, pending_readback) -> VerifiedAnalysis. Runs the LLM call with one retry on validation failure, then the deterministic post-verification from 6.1 (quote substring check, number check that sets verified, prior-range rejection). Add oracle mode.
3. Extend app/agent/nlg.py with LLM mode: generate a template, run template_guard, retry once, then fall back to the deterministic template; then render and run rendered_guard.
Tests with FakeLLM: a hallucinated quote is dropped; "about two-fifty" gives verified False (so TENTATIVE); $250 becomes 25000 cents; invalid JSON is retried; an NLG template containing a digit or an unknown placeholder falls back to the deterministic template.
Also write tests/live/test_nlu_live.py (skipped unless PARLEY_LIVE=1) with 15 realistic rep utterances and expected extractions. Run it once with the demo profile and record accuracy in PROGRESS.md.
Acceptance: offline tests green. Commit.
```

## Phase 7: session, orchestrator, CLI

```
Phase 7 of docs/PHASES.md. Read docs/PLAN.md section 6.5, plus docs/PROGRESS.md Interfaces.
Implement app/agent/session.py (CallSession) and app/agent/orchestrator.py: start(), on_creditor_text(), on_sentence_done(), on_barge_in(), the per-session asyncio.Lock, cancel-and-merge when new text arrives during NLU, effects committed only on the speech ack, affordability via asyncio.to_thread, per-stage timings (nlu_ms, policy_ms, nlg_ms, server_total_ms), and an audit event for every step. Then app/cli.py: `python -m app.cli fixtures/demo`, where you type as the rep; it prints the agent's lines, belief changes, blocks, and the engine verdict, and auto-acks sentences.
Tests with FakeLLM or oracle NLU and template NLG: barge-in drops a pending counter (the next turn re-offers the same counter); a contradiction leads to a clarify question; private-info requests are refused then escalated; a full scripted call reaches PROPOSE_WRAP with a draft agreement that passes the validator.
Acceptance: tests green; a manual CLI call with the demo profile reaches PROPOSE_WRAP (paste a short transcript into PROGRESS.md). Commit.
```

## Phase 8: simulator, scenarios, offline e2e

```
Phase 8 of docs/PHASES.md. Read docs/PLAN.md section 10 (simulator and scenario parts), plus docs/PROGRESS.md Interfaces.
Implement sim/scenarios.py (seeded generate(n, seed) with ground truth and balanced strata), sim/personas.py (flexible, contradictory, pressuring), and sim/creditor.py (code-based CreditorPolicy that sees only the agent's Action intent and PUBLIC facts plus text; checks CONFIRM_SCHEDULE with the validator under the true rules; concedes 5 points per counter down to the floor; plus a phraser using the sim role or templates, which also emits the ground-truth TurnAnalysis for oracle mode). sim/ must not import app.agent: put any shared types it needs in app/domain.
Tests: the generator is deterministic for a seed and hits every stratum; tests/e2e/test_text_call.py runs full offline calls (oracle NLU, template NLG, template sim) for each persona and each stratum and asserts the outcome, a valid agreement under the true rules, zero leaks, and escalation for pressuring.
Acceptance: e2e green with no network. Commit.
```

## Phase 9: eval runner and metrics

```
Phase 9 of docs/PHASES.md. Read docs/PLAN.md section 10 (eval part) and 7a (profiles and budget), plus docs/PROGRESS.md Interfaces.
Implement eval/run_eval.py (--scenarios, --seed, --resume RUN_ID, --profile, --nlg llm|template, --sim-phrasing llm|template; per-scenario JSON written as each finishes; skipped_quota handling; run.json with models, the share of calls per model, seed, git sha, and settings), eval/metrics.py (every metric in section 10, with a summary.json and summary.md table), and eval/thresholds.yaml (exit non-zero on failure).
Test metrics.py on hand-built scenario results. Then run, in order:
1. `--scenarios 12 --seed 7 --nlg template --sim-phrasing template --profile eval` (cheap).
2. The same with `--profile local` if Ollama models are pulled.
3. The full LLM run with `--profile eval`.
Paste each summary.md into PROGRESS.md. If a threshold fails, find the root cause and fix it (tests first), not the threshold.
Acceptance: thresholds pass on run 3. Commit.
```

## Phase 10: voice and UI

```
Phase 10 of docs/PHASES.md. Read docs/PLAN.md section 8, plus docs/PROGRESS.md Interfaces for the orchestrator.
Implement app/voice/stt.py (through the LLM client's transcribe), app/voice/ws.py (the /ws/call/{call_id} protocol from section 8), app/main.py (FastAPI serving static files, the WebSocket, GET /metrics/summary), and app/static/index.html plus app.js (vanilla JS: SYNTHETIC DATA banner, @ricky0123/vad-web from a CDN, 16 kHz WAV encoding, speechSynthesis per sentence with sentence_done acks, barge-in with a toggle, text fallback, panels for the transcript with struck-through blocked lines, the terms table with status chips and quote tooltips, the engine verdict with the schedule and a PRIVATE badge on max affordable, per-turn latency, and the audit tail). Write fixtures/demo/rep_card.md for the human playing the rep.
Tests: a WebSocket test with FastAPI's TestClient in text mode (start, text, say events, sentence_done, barge_in).
Acceptance: `uv run uvicorn app.main:app` works; a spoken call with headphones reaches PROPOSE_WRAP; barge-in stops speech; /metrics/summary shows p50/p95. Record the measured latency in PROGRESS.md. Commit.
```

## Phase 11: README and final polish

```
Phase 11 of docs/PHASES.md. Read docs/PROGRESS.md fully and docs/PLAN.md sections 0, 1, and 13.
Write README.md: the problem, the "LLM is untrusted for arithmetic" architecture (mermaid), how to run (setup, keys, profiles, Ollama, CLI, server, eval), the eval results table copied from the latest summary.md with models and seed, the latency table (cloud and local separate), guard statistics, limitations (the engine's structured candidate set, oracle NLU in e2e, the sim sees Actions not only words, browser TTS echo, free-tier model drift), a synthetic-data statement, and an unaffiliated-project note. No third-party assignment text.
Run a final `pytest -q`, `ruff check .`, and the cheap eval. Mark all phases done in PROGRESS.md. Commit.
```
