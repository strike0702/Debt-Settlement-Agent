/**
 * Phase 36: one rule for every user-visible status phrase, badge, label and
 * heading: sentence case (a capital first letter, never an all-lowercase code
 * like "pending client approval" or "known"). Renders the main components
 * with the whole recorded call and scans their rendered strings.
 *
 * Code is exempt on purpose: `<code>` and monospace text (policy reason codes,
 * the JSON editor) show identifiers as they are.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/AppShell";
import { CaseEditor } from "@/components/CaseEditor";
import { ClientLedger } from "@/components/ClientLedger";
import { Conversation } from "@/components/Conversation";
import { DecisionTrace } from "@/components/DecisionTrace";
import { ScenarioBrief } from "@/components/ScenarioBrief";
import { StatePanel } from "@/components/StatePanel";
import { YourAccount } from "@/components/YourAccount";
import { FIXTURE_SCENARIOS } from "@/fixtures";
import type { Lens } from "@/lib/lens";
import { fullCall } from "@/test/fixtureState";
import type { RepAccount, ScenarioBrief as Brief } from "@/types/protocol";

const LABELS = [
  "[data-badge]",
  "h1",
  "h2",
  "h3",
  "h4",
  "th",
  "dt",
  "summary",
  "h5",
  "dd",
  "label",
  "button",
  "[role=radio]",
  "[role=status]",
  "[role=alert]",
  "[role=note]",
  "option",
  "caption",
  "figcaption > span",
  "td",
  "li",
  "p",
].join(",");

/** Strings that start with a lowercase letter, outside code. */
function lowercaseLabels(root: HTMLElement): string[] {
  const out: string[] = [];
  for (const el of root.querySelectorAll<HTMLElement>(LABELS)) {
    if (el.closest("code, .font-mono, blockquote, textarea")) continue;
    const copy = el.cloneNode(true) as HTMLElement;
    copy.querySelectorAll("code, .font-mono").forEach((c) => c.remove());
    const text = (copy.textContent ?? "").replace(/\s+/g, " ").trim();
    if (/^[a-z]/.test(text)) out.push(`<${el.tagName.toLowerCase()}> ${text.slice(0, 60)}`);
  }
  return out;
}

const BRIEF: Brief = {
  id: "easy_deal",
  title: "Easy deal",
  description: "d",
  expected: "deal",
  creditor: { name: "NorthPeak Collections", creditor_balance_cents: 125000, original_balance_cents: 160000 },
  client: {
    as_of_date: "2026-03-01",
    sda_balance_cents: 44000,
    draft_amount_cents: 22000,
    draft_day: 15,
    first_draft_date: "2026-02-15",
    last_draft_date: "2026-04-15",
    upcoming_drafts: 2,
    upcoming_deposits_cents: 44000,
    upcoming_withdrawals_cents: 0,
    upcoming_ledger: [],
    ledger: [
      { date: "2026-02-15", amount_cents: 22000, type: "credit", scheduled: false },
      { date: "2026-03-15", amount_cents: 22000, type: "credit", scheduled: true },
      { date: "2026-03-20", amount_cents: 5000, type: "debit", scheduled: true },
    ],
  },
  firm: { program_fee_bp: 1800, program_fee_cents: 28800, bank_fee_cents: 950 },
};

const REP: RepAccount = {
  id: "easy_deal",
  creditor: { name: "NorthPeak Collections", outstanding_balance_cents: 125000, original_balance_cents: 160000 },
  rules: { max_payments: 8, min_payment_cents: 10000, structure: "even", opening_ask_bp: 4500, floor_bp: 4000, first_payment: "after the last draft" },
};

function renderConsole(lens: Lens) {
  const state = fullCall(lens);
  return render(
    <AppShell
      scenarios={[...FIXTURE_SCENARIOS, { id: "custom:1", title: "Mine", description: "d", expected: "no_deal", suggested: [], custom: true }]}
      selected="easy_deal"
      onSelect={() => {}}
      onWatch={() => {}}
      watchLabel="Watch a call"
      playing={false}
      lens={lens}
      onLens={() => {}}
      theme="light"
      onTheme={() => {}}
      onAddCase={() => {}}
      onEditCase={() => {}}
      onRemoveCase={() => {}}
      conversation={<Conversation messages={state.messages} mic="listening" suggested={["Correct."]} emptyHint="Press start." />}
      trace={lens === "operator" ? <DecisionTrace traces={state.traces} lens={lens} /> : null}
      state={
        <StatePanel
          state={state}
          lens={lens}
          context={
            lens === "operator" ? (
              <>
                <ScenarioBrief brief={BRIEF} lens={lens} />
                <ClientLedger brief={BRIEF} lens={lens} />
              </>
            ) : (
              <YourAccount source={{ kind: "catalog", id: "easy_deal" }} />
            )
          }
        />
      }
    />,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("sentence case (Phase 36)", () => {
  it("holds for every label in the Debt negotiator view", () => {
    const { container } = renderConsole("operator");
    for (const d of container.querySelectorAll("details")) d.open = true;
    expect(screen.getByText("Pending client approval")).toBeInTheDocument();
    expect(lowercaseLabels(container)).toEqual([]);
  });

  it("holds for every label in the Creditor rep view", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(REP), { status: 200 })));
    const { container } = renderConsole("creditor");
    expect(await screen.findByText("Up to 8 payments")).toBeInTheDocument();
    expect(lowercaseLabels(container)).toEqual([]);
  });

  it("holds for the test-case editor and its errors", async () => {
    const errors = [{ path: "client.draft_day", message: "Write the day of the month the deposit lands, 1 to 31." }];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: { message: "x", errors } }), { status: 400 })));
    const { container } = render(
      <CaseEditor mode="add" initialText={'{\n  "client": {\n    "draft_day": 0\n  }\n}'} templateText="{}" onSave={() => {}} onCancel={() => {}} />,
    );
    expect(await screen.findByText("1 problem to fix", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(lowercaseLabels(container)).toEqual([]);
  });

  it("holds for every opened turn of the trace with its details open", () => {
    const { container } = render(<DecisionTrace traces={fullCall("operator").traces} lens="operator" />);
    for (const b of container.querySelectorAll<HTMLButtonElement>("[aria-expanded=false]")) fireEvent.click(b);
    for (const d of container.querySelectorAll("details")) d.open = true;
    expect(container.querySelectorAll("[aria-expanded=true]").length).toBe(7);
    expect(lowercaseLabels(container)).toEqual([]);
  });

  it("the scan does catch a lowercase badge", () => {
    const { container } = render(
      <div>
        <span data-badge="">pending client approval</span>
      </div>,
    );
    expect(lowercaseLabels(container)).toEqual(["<span> pending client approval"]);
  });
});
