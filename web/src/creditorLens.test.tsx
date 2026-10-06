/**
 * Creditor's-eye privacy: render the whole recorded call in the creditor lens
 * and assert no PRIVATE value from the fixture appears anywhere on the page.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { eventsForLens } from "@/App";
import { AppShell } from "@/components/AppShell";
import { Conversation } from "@/components/Conversation";
import { DecisionTrace } from "@/components/DecisionTrace";
import { StatePanel } from "@/components/StatePanel";
import { easyDeal, FIXTURE_SCENARIOS } from "@/fixtures";
import { money, pct } from "@/lib/format";
import type { Lens } from "@/lib/lens";
import { fullCall } from "@/test/fixtureState";

function renderConsole(lens: Lens) {
  const state = fullCall(lens);
  return render(
    <AppShell
      scenarios={FIXTURE_SCENARIOS}
      selected="easy_deal"
      onSelect={() => {}}
      onWatch={() => {}}
      watchLabel="Watch a call"
      playing={false}
      phase={state.phase}
      lens={lens}
      onLens={() => {}}
      theme="light"
      onTheme={() => {}}
      conversation={<Conversation messages={state.messages} mic="off" suggested={[]} emptyHint="" />}
      trace={<DecisionTrace traces={state.traces} lens={lens} />}
      state={<StatePanel state={state} lens={lens} />}
    />,
  );
}

const pv = easyDeal.private_values;
const privateStrings = [
  pct(pv.max_bp),
  money(pv.program_fee_cents_per_row),
  money(pv.bank_fee_cents),
  ...pv.balance_cents.map(money),
  money(pv.program_fee_cents_per_row * 5),
];

function pageText(): string {
  // Open every collapsible so hidden audit rows count too.
  document.querySelectorAll("details").forEach((d) => (d.open = true));
  return document.body.textContent ?? "";
}

describe("creditor lens", () => {
  it("the fixture really contains the private values (operator lens shows them)", () => {
    renderConsole("operator");
    for (const s of privateStrings) expect(pageText()).toContain(s);
  });

  it("renders no private value from the fixture", () => {
    renderConsole("creditor");
    const text = pageText();
    for (const s of privateStrings) expect(text).not.toContain(s);
    expect(text).not.toContain("max_bp");
    expect(text).not.toContain("program_fee");
    expect(text).not.toContain("balance_cents");
  });

  it("replaces private panels with a lock and still shows the public call", () => {
    renderConsole("creditor");
    expect(screen.getAllByTestId("private-lock").length).toBeGreaterThanOrEqual(9);
    expect(screen.getByText("Agreement drafted")).toBeInTheDocument();
    expect(screen.getAllByText("$500.00").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("Audit log"));
    expect(screen.queryByText("affordability")).not.toBeInTheDocument();
  });

  it("shows human terms, not internals (F10–F12, ported from test_app_js_contracts)", () => {
    renderConsole("creditor");
    const terms = screen.getByText("Terms we've heard").closest("div.rounded-xl")!;
    const text = terms.textContent ?? "";
    for (const status of ["known", "tentative", "contradicted", "assumed", "unknown"]) {
      expect(text.toLowerCase()).not.toMatch(new RegExp(`\\b${status}\\b`));
    }
    expect(text).toContain("No special tiers");
    expect(text).not.toContain("[]");
    expect(document.body.textContent).not.toContain("Last agent intent");
    expect(document.body.textContent).not.toContain("US$");
  });

  it("the rep-view filter drops every private field and audit row", () => {
    const rep = eventsForLens(easyDeal.frames.map((f) => f.ev), "creditor");
    const json = JSON.stringify(rep);
    for (const key of ["max_bp", "program_fee_cents", "bank_fee_cents", "balance_cents", "additional_funds", "affordability", "\"private\":true", "\"offending\":["]) {
      expect(json).not.toContain(key);
    }
    expect(rep.some((e) => e.type === "turn_trace")).toBe(true);
  });
});
