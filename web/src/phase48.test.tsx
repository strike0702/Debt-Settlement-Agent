/**
 * Phase 48 UI: the handoff outcome in both views (and nothing private in the
 * Creditor rep view), the end-of-call line, the model badge in the Debt
 * negotiator trace only, the mic going to call-over on a handoff, the URL
 * holding the scenario and view, the case editor's unsaved-text warning, and
 * the theme-color meta following the theme.
 */
import { act, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { CaseEditor } from "@/components/CaseEditor";
import { Conversation } from "@/components/Conversation";
import { DecisionTrace } from "@/components/DecisionTrace";
import { StatePanel } from "@/components/StatePanel";
import { useTheme } from "@/hooks/useTheme";
import { readUrlState, urlWith, useUrlState } from "@/hooks/useUrlState";
import { type CallState, reduceCall } from "@/lib/callState";
import type { Lens } from "@/lib/lens";
import { micEventFor } from "@/lib/mic";
import { callOutcome, endedLine } from "@/lib/outcome";
import { toRepView } from "@/lib/repView";
import { fullCall } from "@/test/fixtureState";
import type { ServerEvent, TurnTraceEvent } from "@/types/protocol";

const REASON = "We hand the call to a person, because the rep's number is above the most we may accept for this client and they will not come down.";
const SAID = "Your number is above what I can accept on this call, so a specialist will review it.";

/** The recorded call with its last turn replaced by a handoff (operator stream). */
function handoffCall(): CallState {
  const op = fullCall("operator");
  const last = op.traces.at(-1)!;
  const trace: TurnTraceEvent = {
    ...last,
    turn: last.turn + 1,
    counter_bp: null,
    decide: { intent: "ESCALATE", reason: "above_accept_line", reason_key: "above_accept_line", reason_text: REASON, reason_short: "Their number is above our limit." },
  };
  const events: ServerEvent[] = [
    { type: "transcript", role: "creditor", text: "Sixty percent, final.", spoken: true, sentence_id: null, blocked: false },
    { type: "transcript", role: "agent", text: `I need to involve someone from our side. ${SAID}`, spoken: false, sentence_id: "s1", blocked: false },
    { type: "phase", phase: "ESCALATE", intent: "ESCALATE", turn: trace.turn },
    { type: "escalate", reason: "above_accept_line", escalate_reason: SAID },
    { ...trace, type: "turn_trace" },
  ];
  return events.reduce(reduceCall, { ...op, agreement: null });
}

/** The same call as the rep stream folds it: no traces at all. */
const asRep = (s: CallState): CallState => ({ ...s, traces: [] });

describe("handoff outcome", () => {
  it("says why in the Debt negotiator view", () => {
    const s = handoffCall();
    expect(callOutcome(s, "operator")).toMatchObject({ kind: "handoff", title: "Handed off to a specialist", reason: REASON, said: SAID });
    render(<StatePanel state={s} lens="operator" />);
    const card = within(screen.getByTestId("handoff-card"));
    expect(card.getByRole("heading", { name: "Handed off to a specialist" })).toBeInTheDocument();
    expect(card.getByText("Specialist follows up")).toBeInTheDocument();
    expect(card.getByText(REASON)).toBeInTheDocument();
    expect(card.getByText(`The agent told the rep: “${SAID}”`)).toBeInTheDocument();
  });

  it("shows the rep only what the agent told them, never the private reason", () => {
    const s = asRep(handoffCall());
    expect(callOutcome(s, "creditor")?.reason).toBeNull();
    // Even if a trace slipped through, the creditor lens never reads it.
    expect(callOutcome(handoffCall(), "creditor")?.reason).toBeNull();
    const { container } = render(<StatePanel state={handoffCall()} lens="creditor" />);
    const card = within(screen.getByTestId("handoff-card"));
    expect(card.getByText(`What the agent told you: “${SAID}”`)).toBeInTheDocument();
    expect(card.getByText(/No deal was agreed on this call/)).toBeInTheDocument();
    for (const secret of [REASON, "above the most we may accept", "above our limit", "above_accept_line"]) {
      expect(container.textContent).not.toContain(secret);
    }
  });

  it("drops the policy's reason code from the rep view, as the server does", () => {
    expect(toRepView({ type: "escalate", reason: "above_accept_line", escalate_reason: SAID })).toEqual({
      type: "escalate",
      reason: null,
      escalate_reason: SAID,
    });
    const row = { type: "audit" as const, id: 1, ts: "", actor: "policy", event: "decide", payload: { intent: "ESCALATE", reason: "infeasible" }, private: false };
    expect(toRepView(row)).toEqual({ ...row, payload: { intent: "ESCALATE" } });
  });

  it.each<Lens>(["operator", "creditor"])("closes the transcript with how the call ended (%s)", (lens) => {
    const s = lens === "operator" ? handoffCall() : asRep(handoffCall());
    expect(endedLine(s, lens)).toBe("Call ended: handed off to a specialist");
    render(<Conversation messages={s.messages} mic="off" suggested={[]} emptyHint="" ended={endedLine(s, lens)} />);
    expect(screen.getByTestId("call-ended")).toHaveTextContent("Call ended: handed off to a specialist");
  });

  it("says a deal went to the client, and nothing while the call runs", () => {
    const deal = fullCall("operator");
    expect(endedLine(deal, "operator")).toBe("Call ended: deal sent to the client for approval");
    expect(endedLine({ ...deal, phase: "NEGOTIATE", autoplay: null }, "operator")).toBeNull();
  });

  it("turns the mic off on a handoff as on END", () => {
    expect(micEventFor({ type: "phase", phase: "ESCALATE", intent: null, turn: 6 })).toEqual({ type: "call_over" });
    expect(micEventFor({ type: "phase", phase: "END", intent: null, turn: 6 })).toEqual({ type: "call_over" });
    expect(micEventFor({ type: "phase", phase: "NEGOTIATE", intent: null, turn: 6 })).toEqual({ type: "agent_done" });
  });
});

describe("model badge", () => {
  const reader = { kind: "llm" as const, provider: "anthropic", model: "claude-haiku-5-5", fallback: false, budget_reached: false, cache_hit: false };
  const traces = () => fullCall("operator").traces.map((t) => (t.creditor_text ? { ...t, reader } : t));

  it("shows which model read the rep's line in an open row", () => {
    render(<DecisionTrace traces={traces()} lens="operator" />);
    const badge = screen.getByTestId("reader-badge");
    expect(badge).toHaveTextContent("Read by Claude Haiku");
    expect(badge).toHaveAttribute("title", "anthropic/claude-haiku-5-5");
  });

  it("never shows it in the Creditor rep lens", () => {
    render(<DecisionTrace traces={traces()} lens="creditor" />);
    expect(screen.queryByTestId("reader-badge")).toBeNull();
    expect(document.body.textContent).not.toContain("Claude");
  });
});

describe("URL state", () => {
  beforeEach(() => window.history.replaceState(null, "", "/?fixture=1"));
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("reads and writes ?scenario= and ?view=, keeping other parameters", () => {
    expect(readUrlState("?scenario=no_space&view=rep")).toEqual({ scenario: "no_space", lens: "creditor" });
    expect(readUrlState("?view=bogus")).toEqual({ scenario: null, lens: null });
    expect(urlWith("http://x/?fixture=1", { scenario: "easy_deal", lens: "operator" })).toBe("http://x/?fixture=1&scenario=easy_deal&view=operator");
  });

  it("replaces on first render, pushes on a change, and follows back/forward", () => {
    let picked = "easy_deal";
    let lens: Lens = "operator";
    const onScenario = (id: string) => ((picked = id), true);
    const onLens = (l: Lens) => (lens = l);
    const { rerender } = renderHook((p: { scenario: string; lens: Lens }) => useUrlState({ ...p, onScenario, onLens }), {
      initialProps: { scenario: picked, lens } as { scenario: string; lens: Lens },
    });
    const start = window.history.length;
    expect(window.location.search).toBe("?fixture=1&scenario=easy_deal&view=operator");
    rerender({ scenario: "no_space", lens: "creditor" });
    expect(window.location.search).toBe("?fixture=1&scenario=no_space&view=rep");
    expect(window.history.length).toBe(start + 1);
    act(() => {
      window.history.replaceState(null, "", "/?fixture=1&scenario=easy_deal&view=operator");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect([picked, lens]).toEqual(["easy_deal", "operator"]);
  });

  it("puts the URL back when App refuses a scenario from back/forward", () => {
    renderHook(() => useUrlState({ scenario: "easy_deal", lens: "operator", onScenario: () => false, onLens: () => {} }));
    act(() => {
      window.history.replaceState(null, "", "/?scenario=no_space&view=operator");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(readUrlState().scenario).toBe("easy_deal");
  });
});

describe("case editor", () => {
  it("warns before leaving with unsaved text, and not before", () => {
    render(<CaseEditor mode="add" initialText="{}" templateText="{}" onSave={() => {}} onCancel={() => {}} />);
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: '{"a": 1}' } });
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });
});

describe("theme", () => {
  it("keeps the theme-color meta on the page background", () => {
    const meta = document.createElement("meta");
    meta.name = "theme-color";
    meta.content = "#f6f6f4";
    document.head.appendChild(meta);
    document.documentElement.classList.remove("dark");
    const { result } = renderHook(() => useTheme());
    act(() => result.current[1]());
    expect(meta.content).toBe("#121211");
    act(() => result.current[1]());
    expect(meta.content).toBe("#f6f6f4");
    meta.remove();
  });
});
