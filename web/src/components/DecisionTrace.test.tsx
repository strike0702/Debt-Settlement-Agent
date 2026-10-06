import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DecisionTrace } from "@/components/DecisionTrace";
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

  it("locks the engine step in the creditor lens even if a trace carries affordability", () => {
    // Unfiltered operator traces: the component itself must refuse to draw them.
    const { traces } = fullCall("operator");
    render(<DecisionTrace traces={traces} lens="creditor" />);
    expect(screen.getAllByTestId("private-lock")).toHaveLength(7);
    expect(screen.queryByText(/Ceiling/)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("52%");
  });
});
