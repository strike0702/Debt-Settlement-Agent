# A/B pre-registration (Phase 24)

Written before the A/B ran, as section 2(c) of the review plan (review dated 2026-10-06/07, commit `7a45733`; the plan itself was retired after all its phases shipped). Kept here unchanged so the adoption rule the A/B applied can be checked. The "cut order" referenced in [decision.md](decision.md) follows.

## (c) ReAct / tool-calling agent: decision

**Decision: keep code-owned policy for every move. Do not adopt ReAct or "LLM picks among approved moves" in `app/`. Ship a conversational-NLG upgrade (H3) instead, and prove the choice with a three-arm A/B in which the ReAct agent is a real, fairly built competitor.**

**What ReAct would gain:**
- answering off-script questions;
- combining an acknowledgement with a move;
- handling multi-intent turns;
- recovering from NLU misses;
- more varied phrasing.

**What it would risk, on this codebase:**
- **Numbers.** The LLM composes figures. Either the guards fire more often, giving more "Let me check that figure" fallbacks, or the guards are loosened and the core claim is lost.
- **Privacy.** A ReAct agent must see the private ceiling to negotiate. Today the NLG never does. That is a structural guarantee traded for a prompt instruction.
- **Latency.** Reason → tool → reason → answer is 2–4 sequential LLM calls per turn. At the measured 0.5–2 s each, that adds 1.5–5 s, directly against (b).
- **Eval and CI.** The keyless CI eval needs deterministic moves, and a ReAct agent's results vary run to run.
- **Auditability.** Reason codes like `rep_firm` or `max_counters` become prose. "Why 58%?" no longer has a testable answer.

**Hybrids considered:**
- **H1: LLM picks among policy-approved moves.** Move choice is already where the policy scores 1.0, with 0.689 surplus. Smoothness problems come from *phrasing and one-act turns*. Low gain, and determinism lost. Rejected.
- **H2: LLM-driven dialogue with policy as a guarded tool.** The most natural option, but it carries every risk above. Kept as an **eval-only** arm.
- **H3: policy decides, the LLM composes a richer reply. Chosen.**
  - `decide()` stays authoritative.
  - Actions gain an optional **acknowledgement act** that echoes verified creditor terms, built from PUBLIC creditor facts.
  - A small `ANSWER` intent with policy-supplied, number-free talking points handles off-script questions.
  - NLG sees the last 3 public turns instead of 1.
  - Same placeholders and guards, at most 1 NLG call (0 with the bank).
  - **No CLAUDE.md rule is broken.**

**How we prove it (A/B on the existing harness):**
- Arms on identical seeds:
  - **A**, today: policy + template NLG.
  - **B**: policy + H3.
  - **C**: ReAct tool-calling agent. Its tools map one-to-one onto `Action` (`evaluate_offer`, `propose_counter`, `propose_terms`, `confirm_schedule`, `refuse_private`, `escalate`, `end_no_deal`), so `CreditorPolicy.respond` works unchanged. It is given the private ceiling.
  - Optionally **D**, a pure LLM-only baseline.
- **Explicit rule break, eval-only:** arms C and D let an LLM choose moves and write numbers. That is what is being measured. They live under `eval/agents/` and are never imported by `app/`.
- **Harder conditions** (fixes F8): live NLU, `--sim-phrasing llm`, no oracle overlay, n=48.
- **Metrics:**
  - existing hard metrics: leaks, unverified figures, agreement validity, deal/no-deal/escalation rates, counters, surplus, turns;
  - new: **LLM calls per turn** and **server_total p50/p95**;
  - **naturalness:** blind pairwise LLM-judge win-rate, plus 20 transcripts rated by the user to check the judge.
  - This deliberately reverses ROADMAP §6's "no LLM-as-judge", for this secondary metric only, never as a gate.
- **Pre-registered adoption rule** for any arm over A:
  - zero leaks and zero unverified figures;
  - `agreement_valid` = 1;
  - outcome rates within A's 95% CI;
  - p50 latency ≤ A + 0.5 s;
  - naturalness win-rate ≥ 60%.




## Cut order if time runs short
Cut from the end of this list first, so the latency win and the corrections always ship:

P24b naturalness judge → P24a arm D (LLM-only) → P23b mobile layout → P22 autoplay → P21 template bank (keep the limiter, timeouts, and effort fixes) → P25 keep-warm.



