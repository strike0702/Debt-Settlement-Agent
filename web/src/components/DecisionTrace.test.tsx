/**
 * "How the agent decided" (Phase 36 final design) over the recorded easy_deal
 * call: one row per turn with a plain title, key number and a few-word reason;
 * the newest row open with the five-line story; internals one click further;
 * each safety check once; no stance labels, raw codes or "None" up front; one
 * lock in the Creditor rep view. Also the plain-words helpers in traceStory.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DecisionTrace, shortReason } from "@/components/DecisionTrace";
import { affordLine, checkSummary, heardLines, turnTitle } from "@/lib/traceStory";
import { fullCall } from "@/test/fixtureState";

const traces = () => fullCall("operator").traces;
const row = (turn: number) => screen.getByRole("listitem", { name: `Turn ${turn}` });
const open = (turn: number) => {
  const button = within(row(turn)).getAllByRole("button")[0]!;
  if (button.getAttribute("aria-expanded") !== "true") fireEvent.click(button);
  return within(row(turn));
};

/** Visible text outside <details>, <code> and monospace. */
function visibleText(root: HTMLElement): string {
  const copy = root.cloneNode(true) as HTMLElement;
  copy.querySelectorAll("details, code, .font-mono").forEach((n) => n.remove());
  return copy.textContent ?? "";
}

describe("DecisionTrace", () => {
  it("lists every turn newest first with a plain title, number and short reason", () => {
    render(<DecisionTrace traces={traces()} lens="operator" />);
    expect(screen.getByRole("heading", { name: "How the agent decided" })).toBeInTheDocument();
    expect(screen.getByText("Every decision comes from code; the AI handles only the language.")).toBeInTheDocument();
    const rows = screen.getAllByTestId("turn-card");
    expect(rows).toHaveLength(7);
    expect(rows[0]).toHaveAccessibleName("Turn 6");
    expect(rows.at(-1)).toHaveAccessibleName("Turn 0");
    const turn2 = within(row(2));
    expect(turn2.getByText("Countered at 32%")).toBeInTheDocument();
    expect(turn2.getByText("32%")).toBeInTheDocument();
    expect(turn2.getByText("A step toward their ask that the client can afford.")).toBeInTheDocument();
  });

  it("opens the newest turn by default and toggles rows", () => {
    render(<DecisionTrace traces={traces()} lens="operator" />);
    const [newest, next] = screen.getAllByTestId("turn-card");
    expect(within(newest!).getAllByRole("button")[0]).toHaveAttribute("aria-expanded", "true");
    const button = within(next!).getByRole("button");
    expect(button).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(within(next!).getByText("What we said")).toBeInTheDocument();
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("tells an open turn as a story with plain row labels", () => {
    render(<DecisionTrace traces={traces()} lens="operator" />);
    const c = open(2);
    for (const label of ["What they said", "What we heard", "Can the client pay?", "Decision", "What we said"]) {
      expect(c.getByText(label)).toBeInTheDocument();
    }
    expect(c.getByText("forty-five percent of the balance", { selector: "mark" })).toBeInTheDocument();
    expect(c.getByText("They want 45% of the balance.")).toBeInTheDocument();
    expect(c.getByText(/^Yes\. 32% fits the client's savings \(the most is 52%\)\.$/)).toBeInTheDocument();
    expect(c.getByText(/^We offer 32% \(\$400\.00\)\./)).toBeInTheDocument();
    expect(c.getByText("We can propose 32% of the balance, which is $400.00. Would that work?")).toBeInTheDocument();
    // The short reason is not repeated once the full decision is showing.
    expect(c.queryByText("A step toward their ask that the client can afford.")).not.toBeInTheDocument();
  });

  it("hides empty lines: the opening has no rep line, nothing heard and no affordability", () => {
    render(<DecisionTrace traces={traces()} lens="operator" />);
    const c = open(0);
    expect(c.queryByText("What they said")).not.toBeInTheDocument();
    expect(c.queryByText("What we heard")).not.toBeInTheDocument();
    expect(c.getByText("Decision")).toBeInTheDocument();
  });

  it("keeps internals one click away, with each safety check once", () => {
    render(<DecisionTrace traces={traces()} lens="operator" />);
    const c = open(3);
    const details = row(3).querySelector("details")!;
    expect(details.open).toBe(false);
    details.open = true;
    const d = within(details);
    expect(d.getByText("COUNTER", { selector: "code" })).toBeInTheDocument();
    expect(d.getByText("bp=3700", { selector: "code" })).toBeInTheDocument();
    expect([...details.querySelectorAll("[data-placeholder]")].map((s) => s.textContent)).toEqual(["{counter_pct}", "{offer_total}"]);
    expect(d.getAllByText(/No unchecked numbers or promises/)).toHaveLength(1);
    expect(d.getByText(/stopped a line \(number not from facts\)/)).toBeInTheDocument();
    expect(d.getByText("The safe template was spoken instead of the AI's wording.")).toBeInTheDocument();
    expect(c.getAllByText(/Which settlements the client can afford/)).toHaveLength(1);
  });

  it("shows no stance labels, raw codes or empty placeholders up front", () => {
    const { container } = render(<DecisionTrace traces={traces()} lens="operator" />);
    for (const t of [1, 2, 3, 4, 5]) open(t);
    const text = visibleText(container);
    for (const raw of ["COUNTER", "CONFIRM_SCHEDULE", "None.", "bp=", "{counter_pct}", "KNOWN", "Made an offer", "Gave account details", "Pushed back"]) {
      expect(text).not.toContain(raw);
    }
  });

  it("shows each safety check once even when the server repeats it per sentence", () => {
    const t = traces().find((x) => x.turn === 2)!;
    const ok = { stage: "rendered" as const, ok: true, reason: null, offending: null };
    const repeated = { ...t, nlg: { ...t.nlg, guards: [...t.nlg.guards, ok, ok, ok] } };
    render(<DecisionTrace traces={[repeated]} lens="operator" />);
    const details = row(2).querySelector("details")!;
    details.open = true;
    expect(within(details).getAllByText("No unchecked numbers or promises")).toHaveLength(1);
  });

  it("explains itself when there are no turns, or shows the caller's note", () => {
    const { rerender } = render(<DecisionTrace traces={[]} lens="operator" />);
    expect(screen.getByText(/Each turn of the call appears here/)).toBeInTheDocument();
    rerender(<DecisionTrace traces={[]} lens="operator" note="The server no longer has this call." />);
    expect(screen.getByText("The server no longer has this call.")).toBeInTheDocument();
  });

  it("is one lock in the creditor lens even if handed operator traces", () => {
    render(<DecisionTrace traces={traces()} lens="creditor" />);
    expect(screen.getAllByTestId("private-lock")).toHaveLength(1);
    expect(screen.queryAllByTestId("turn-card")).toHaveLength(0);
    expect(document.body.textContent).not.toContain("52%");
  });

  it("falls back to the full reason on frames without a short one", () => {
    const t = traces().find((x) => x.turn === 2)!;
    expect(shortReason({ ...t, decide: { ...t.decide, reason_short: undefined } })).toBe(t.decide.reason_text);
  });
});

describe("traceStory", () => {
  const byTurn = () => new Map(traces().map((t) => [t.turn, t]));

  it("titles moves in plain words with their number", () => {
    const m = byTurn();
    expect(turnTitle(m.get(0)!)).toBe("Opened the call");
    expect(turnTitle(m.get(1)!)).toBe("Asked what they would settle for");
    expect(turnTitle(m.get(2)!)).toBe("Countered at 32%");
    expect(turnTitle(m.get(4)!)).toBe("Accepted 40%");
    expect(turnTitle(m.get(5)!)).toBe("Sent the deal to the client");
    expect(turnTitle(m.get(6)!)).toBe("Closed the call");
  });

  it("retells what was heard, and whether the client can pay", () => {
    const m = byTurn();
    expect(heardLines(m.get(1)!)).toEqual(["Up to 8 payments", "At least $100 per payment", "All payments the same amount"]);
    expect(affordLine({ ...m.get(1)!, affordability: null, needs_info: ["max_payments"] })?.text).toBe(
      "Not checked yet: the agent still needs max payments.",
    );
    expect(affordLine({ ...m.get(0)!, affordability: null })).toBeNull();
    const full = { ...m.get(2)!, affordability: { ...m.get(2)!.affordability!, max_bp: 10000 } };
    expect(affordLine(full)?.text).toBe("Yes. 32% fits the client's savings (even the full balance would).");
  });

  it("counts a check that failed on any sentence as failed, once", () => {
    const t = traces().find((x) => x.turn === 3)!;
    const ok = { stage: "rendered" as const, ok: true, reason: null };
    const c = checkSummary({ ...t, nlg: { ...t.nlg, guards: [...t.nlg.guards, ok, ok] } });
    expect(c.passed).toEqual(["Uses an approved reply"]);
    expect(c.blocked).toEqual(["No unchecked numbers or promises: stopped a line (number not from facts)"]);
    expect(c.fallback).toBe(true);
  });
});
