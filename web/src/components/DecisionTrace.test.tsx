import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DecisionTrace, engineNote } from "@/components/DecisionTrace";
import { fullCall } from "@/test/fixtureState";

describe("DecisionTrace", () => {
  it("renders one card per agent turn, newest first", () => {
    const { traces } = fullCall("operator");
    render(<DecisionTrace traces={traces} lens="operator" />);
    const cards = screen.getAllByTestId("turn-card");
    expect(cards).toHaveLength(7);
    expect(cards[0]).toHaveAccessibleName("Turn 6");
    expect(cards.at(-1)).toHaveAccessibleName("Turn 0");
  });

  it("shows every pipeline step for a counter turn", () => {
    const { traces } = fullCall("operator");
    render(<DecisionTrace traces={traces} lens="operator" />);
    const card = screen.getByRole("article", { name: "Turn 2" });
    const c = within(card);

    // 1. rep line with the verified ask quote highlighted
    expect(c.getByText("forty-five percent of the balance", { selector: "mark" })).toBeInTheDocument();
    // 2. NLU
    expect(c.getByText("45%")).toBeInTheDocument();
    // 4. engine sparkline legend with the private ceiling marked
    expect(c.getByText(/Ceiling 52%/)).toBeInTheDocument();
    expect(c.getByText(/Ours 32%/)).toBeInTheDocument();
    // 5. policy reason in plain English plus the code
    expect(c.getByText("Counter at 32%: the first offer anchors at 70% of their ask.")).toBeInTheDocument();
    expect(c.getByText("COUNTER · bp=3200")).toBeInTheDocument();
    // 6. template placeholders highlighted, guards passed
    const slots = card.querySelectorAll("[data-placeholder]");
    expect([...slots].map((s) => s.textContent)).toEqual(["{counter_pct}", "{offer_total}"]);
    expect(c.getByText(/rendered passed/)).toBeInTheDocument();
    // 7. spoken line
    expect(c.getByText("We can propose 32% of the balance, which is $400.00. Would that work?")).toBeInTheDocument();
  });

  it("strikes through dropped NLU terms and shows the reason", () => {
    const { traces } = fullCall("operator");
    render(<DecisionTrace traces={traces} lens="operator" />);
    const card = screen.getByRole("article", { name: "Turn 1" });
    const struck = card.querySelector("s");
    expect(struck).toHaveTextContent("First payment Nov 1, 2026");
    expect(within(card).getByText("dropped: quote not in utterance")).toBeInTheDocument();
    for (const q of ["up to eight monthly payments", "at least one hundred dollars each", "all the same amount"]) {
      expect(within(card).getByText(q, { selector: "mark" })).toBeInTheDocument();
    }
  });

  it("shows a blocked guard and the template fallback", () => {
    const { traces } = fullCall("operator");
    render(<DecisionTrace traces={traces} lens="operator" />);
    const c = within(screen.getByRole("article", { name: "Turn 3" }));
    expect(c.getByText(/rendered blocked: number not from facts/)).toBeInTheDocument();
    expect(c.getByText(/template fallback spoken/)).toBeInTheDocument();
  });

  it("explains itself when there are no turns yet", () => {
    render(<DecisionTrace traces={[]} lens="operator" />);
    expect(screen.getByText(/Each agent turn appears here/)).toBeInTheDocument();
  });

  it("is one lock in the creditor lens even if handed operator traces (Phase 35)", () => {
    // Unfiltered operator traces: the component itself must refuse to draw them.
    const { traces } = fullCall("operator");
    render(<DecisionTrace traces={traces} lens="creditor" />);
    expect(screen.getAllByTestId("private-lock")).toHaveLength(1);
    expect(screen.queryAllByTestId("turn-card")).toHaveLength(0);
    expect(screen.queryByText(/Ceiling/)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("52%");
  });

  it("says what the rep did in plain English in step 1 (Phase 35)", () => {
    const { traces } = fullCall("operator");
    render(<DecisionTrace traces={traces} lens="operator" />);
    expect(document.body.textContent).not.toContain("rep stance");
    const turn2 = within(screen.getByRole("article", { name: "Turn 2" }));
    expect(turn2.getByRole("heading", { name: "Creditor rep said · made an offer" })).toBeInTheDocument();
    // The opening has no rep line, so no stance phrase.
    const turn0 = within(screen.getByRole("article", { name: "Turn 0" }));
    expect(turn0.getByRole("heading", { name: "Creditor rep said" })).toBeInTheDocument();
  });

  it("explains a turn with no engine run instead of a bare message (Phase 35)", () => {
    const { traces } = fullCall("operator");
    const t1 = traces.find((t) => t.turn === 1)!;
    const waiting = { ...t1, turn: 1, affordability: null, needs_info: ["max_payments", "min_payment_cents"] };
    render(<DecisionTrace traces={[waiting]} lens="operator" />);
    expect(screen.getByTestId("engine-note")).toHaveTextContent("Waiting for: max payments, minimum payment.");
  });

  it("engineNote covers the opening, missing rules, a clarify, and old frames", () => {
    expect(engineNote({ creditor_text: null })).toMatch(/opening or closing/);
    expect(engineNote({ creditor_text: "hi", needs_info: ["payment_structure"] })).toMatch(/^Waiting for: structure\./);
    expect(engineNote({ creditor_text: "hi", needs_info: [] })).toMatch(/clarifying question/);
    expect(engineNote({ creditor_text: "hi" })).toBe("No engine run this turn.");
  });

  it("shows the caller's note when there are no traces (rep-stream call)", () => {
    render(<DecisionTrace traces={[]} lens="operator" note="This call streams the Creditor rep view." />);
    expect(screen.getByText("This call streams the Creditor rep view.")).toBeInTheDocument();
    expect(screen.queryByText(/Each agent turn appears here/)).not.toBeInTheDocument();
  });
});
