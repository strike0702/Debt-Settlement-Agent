/**
 * LiveApp scenario picker over mocked `fetch` and `WebSocket`: the picker is
 * locked while a call runs and unlocks once the call is finished, even though
 * a finished autoplay call leaves its socket open (Phase 23b fix). The
 * Creditor rep view swaps the operator brief for the rep's own account
 * (Phase 34). Phase 35: view names, the client ledger in the Debt negotiator
 * view, no decision trace in the Creditor rep view. Phase 36: a call started in
 * the Creditor rep view still shows its decision trace in the Debt negotiator
 * view (backfilled from `/calls/{id}/operator`), and custom test cases can be
 * added, persisted, edited, removed and started.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { easyDeal } from "@/fixtures";
import type { OperatorDetail, RepAccount, ScenarioBrief, ScenarioMeta, ServerEvent, TurnTraceEvent } from "@/types/protocol";

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  sent: string[] = [];
  send(data: string): void {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  emit(ev: ServerEvent) {
    this.onmessage?.({ data: JSON.stringify(ev) });
  }
}

const SCENARIOS: ScenarioMeta[] = [
  { id: "easy_deal", title: "Easy deal", description: "d", expected: "deal", suggested: [] },
  { id: "no_space", title: "No space", description: "d", expected: "no_deal", suggested: [] },
];

const REP: RepAccount = {
  id: "easy_deal",
  creditor: { name: "NorthPeak Collections", outstanding_balance_cents: 125000, original_balance_cents: 160000 },
  rules: { max_payments: 8, min_payment_cents: 10000, structure: "even", opening_ask_bp: 4500, floor_bp: 4000, first_payment: null },
};

const BRIEF: ScenarioBrief = {
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
    first_draft_date: "2026-03-15",
    last_draft_date: "2026-04-15",
    upcoming_drafts: 2,
    upcoming_deposits_cents: 44000,
    upcoming_withdrawals_cents: 0,
    upcoming_ledger: [],
    ledger: [
      { date: "2026-03-15", amount_cents: 22000, type: "credit", scheduled: true },
      { date: "2026-04-15", amount_cents: 22000, type: "credit", scheduled: true },
    ],
  },
  firm: { program_fee_bp: 1800, program_fee_cents: 28800, bank_fee_cents: 950 },
};

const TRACES = easyDeal.frames.map((f) => f.ev).filter((e): e is TurnTraceEvent => e.type === "turn_trace");
const TEMPLATE = {
  meta: { id: "custom", title: "My test case", description: "Mine", expected: "deal" },
  offer: { creditor: "NorthPeak Collections", creditor_balance_cents: 125000, original_balance_cents: 160000 },
  firm: { program_fee_pct: 0.18, bank_fee_cents: 950 },
  client: { draft_amount_cents: 22000 },
};
let operatorDetail: OperatorDetail | null = null;
let previewStatus = 200;

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
  if (url === "/scenarios") return json(SCENARIOS);
  if (url === "/scenarios/easy_deal/rep") return json(REP);
  if (url === "/scenarios/easy_deal") return json(BRIEF);
  if (url === "/scenarios/template") return json(TEMPLATE);
  if (url === "/scenarios/preview")
    return previewStatus === 200
      ? json(BRIEF)
      : json({ detail: { message: "x", errors: [{ path: "client.draft_day", message: "Write the day of the month the deposit lands, 1 to 31." }] } }, 400);
  if (url === "/scenarios/preview/rep") return json({ ...REP, id: "custom", suggested: ["Correct."] });
  if (/^\/calls\/[^/]+\/operator$/.test(url)) return operatorDetail ? json(operatorDetail) : json({ detail: "gone" }, 404);
  return new Response("null", { status: 404 });
});

beforeEach(() => {
  FakeSocket.all = [];
  fetchMock.mockClear();
  operatorDetail = null;
  previewStatus = 200;
  localStorage.clear();
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const card = (title: string) => screen.getByRole("button", { name: new RegExp(title) });

describe("LiveApp scenario picker", () => {
  it("is locked during an autoplay call and unlocks after autoplay_done", async () => {
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Watch a call/ }));
    const ws = FakeSocket.all.at(-1)!;
    act(() => ws.open());

    fireEvent.click(card("No space"));
    expect(card("Easy deal")).toHaveAttribute("aria-pressed", "true");

    act(() =>
      ws.emit({ type: "autoplay_done", final_intent: "CLOSE", outcome: "deal", phase: "END", turns: 6 } as ServerEvent),
    );
    fireEvent.click(card("No space"));
    expect(card("No space")).toHaveAttribute("aria-pressed", "true");
    expect(card("Easy deal")).toHaveAttribute("aria-pressed", "false");
  });
});

describe("LiveApp Creditor rep view", () => {
  it("shows the rep's own account instead of the operator brief", async () => {
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();
    expect(screen.queryByText("Your account")).not.toBeInTheDocument();
    const briefCalls = () => fetchMock.mock.calls.filter(([u]) => u === "/scenarios/easy_deal").length;
    const before = briefCalls();

    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(await screen.findByText("Up to 8 payments")).toBeInTheDocument();
    expect(screen.getByText("Your account")).toBeInTheDocument();
    expect(screen.getByText("Floor 40% — don't go below")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/scenarios/easy_deal/rep");
    // The private brief is never requested in the Creditor rep view.
    expect(briefCalls()).toBe(before);
  });

  it("has no decision trace column and no client ledger (Phase 35)", async () => {
    render(<App />);
    expect(await screen.findByText("Client deposits and credits")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(await screen.findByText("Your account")).toBeInTheDocument();
    expect(screen.queryByText("How the agent decided")).not.toBeInTheDocument();
    expect(screen.queryByText("Client deposits and credits")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("$220.00");
  });
});

describe("LiveApp view names and Debt negotiator view (Phase 35)", () => {
  it("names the views in plain words with a one-line subtitle", async () => {
    render(<App />);
    expect(await screen.findByRole("radiogroup", { name: "View" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Debt negotiator" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText(/Operator|Creditor's eye/)).not.toBeInTheDocument();
    expect(screen.getByTestId("view-hint")).toHaveTextContent(/client's money/);
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(screen.getByTestId("view-hint")).toHaveTextContent(/creditor's representative/);
  });

  it("shows the client ledger next to the brief", async () => {
    render(<App />);
    expect(await screen.findByText("Client deposits and credits")).toBeInTheDocument();
    expect(screen.getByText("Balance today (as of)")).toBeInTheDocument();
    expect(screen.getByText("$880.00")).toBeInTheDocument();
  });

  it("shows the decision trace for a call started in the Creditor rep view (Phase 36)", async () => {
    // Regression: the trace column used to say the call "carries no decision trace".
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    fireEvent.click(screen.getByRole("button", { name: /Watch a call/ }));
    const ws = FakeSocket.all.at(-1)!;
    expect(ws.url).toContain("view=rep");
    const callId = decodeURIComponent(/\/ws\/call\/([^?]+)/.exec(ws.url)![1]!);
    operatorDetail = { call_id: callId, traces: TRACES.slice(0, 3), audit: [], eval: null, agreement: null };
    act(() => ws.open());
    act(() => ws.emit({ type: "transcript", role: "agent", text: "Hello.", spoken: true, sentence_id: "s1", blocked: false }));
    act(() => ws.emit({ type: "turn_done" }));
    // No trace is fetched while the rep view is open.
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/operator"))).toBe(false);

    fireEvent.click(screen.getByRole("radio", { name: "Debt negotiator" }));
    expect(await screen.findAllByTestId("turn-card")).toHaveLength(3);
    expect(screen.queryByText(/carries no decision trace/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(`/calls/${encodeURIComponent(callId)}/operator`);

    // Each new turn refetches, so the trace stays current.
    operatorDetail = { ...operatorDetail, traces: TRACES.slice(0, 5) };
    act(() => ws.emit({ type: "turn_done" }));
    await waitFor(() => expect(screen.getAllByTestId("turn-card")).toHaveLength(5));
  });

  it("says so when the server no longer has a rep-view call's trace", async () => {
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    fireEvent.click(screen.getByRole("button", { name: /Watch a call/ }));
    const ws = FakeSocket.all.at(-1)!;
    act(() => ws.open());
    act(() => ws.emit({ type: "turn_done" }));
    fireEvent.click(screen.getByRole("radio", { name: "Debt negotiator" }));
    expect(await screen.findByText(/no longer has this call's decision trace/)).toBeInTheDocument();
  });

  it("has no call-phase badge in the header (Phase 36)", async () => {
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Start call/ }));
    const ws = FakeSocket.all.at(-1)!;
    act(() => ws.open());
    act(() => ws.emit({ type: "phase", phase: "NEGOTIATE", intent: "COUNTER", turn: 2 } as ServerEvent));
    expect(screen.queryByLabelText(/Call phase/)).not.toBeInTheDocument();
    expect(within(screen.getByRole("banner")).queryByText("NEGOTIATE")).not.toBeInTheDocument();
  });
});

describe("LiveApp custom test cases (Phase 36)", () => {
  async function addCase() {
    render(<App />);
    expect(await screen.findByRole("button", { name: /No space/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add a test case" }));
    const editor = (await screen.findByLabelText("Test case JSON")) as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toContain('"NorthPeak Collections"'));
    return editor;
  }

  it("opens the editor pre-filled with the template and adds the case as a card", async () => {
    await addCase();
    fireEvent.click(screen.getByRole("button", { name: "Add test case" }));
    const card = await screen.findByRole("button", { name: /^My test case/ });
    expect(card).toHaveAttribute("aria-pressed", "true");
    expect(within(card).getByText("Custom")).toBeInTheDocument();
    expect(screen.queryByLabelText("Test case JSON")).not.toBeInTheDocument();
    // Autoplay is curated-only, and the page says so.
    expect(screen.getByRole("button", { name: /Watch a call/ })).toBeDisabled();
    expect(screen.getAllByText(/only knows the built-in cases/).length).toBeGreaterThan(0);
    const stored = JSON.parse(localStorage.getItem("dsa-custom-cases") ?? "[]") as { title: string; suggested: string[] }[];
    expect(stored.map((c) => c.title)).toEqual(["My test case"]);
    expect(stored[0]!.suggested).toEqual(["Correct."]);
  });

  it("starts a call on a custom case with its JSON as scenario_payload", async () => {
    await addCase();
    fireEvent.click(screen.getByRole("button", { name: "Add test case" }));
    await screen.findByRole("button", { name: /^My test case/ });
    fireEvent.click(screen.getByRole("button", { name: /Start call/ }));
    const ws = FakeSocket.all.at(-1)!;
    act(() => ws.open());
    const start = JSON.parse(ws.sent[0]!) as Record<string, unknown>;
    expect(start).toEqual({ type: "start", scenario_id: "custom", scenario_payload: TEMPLATE });
  });

  it("shows server errors with their field path and does not add the case", async () => {
    previewStatus = 400;
    await addCase();
    fireEvent.click(screen.getByRole("button", { name: "Add test case" }));
    const errors = await screen.findByTestId("case-errors");
    expect(within(errors).getByText("client.draft_day")).toBeInTheDocument();
    expect(within(errors).getByText(/1 to 31/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^My test case/ })).not.toBeInTheDocument();
  });

  it("reports a JSON syntax error before calling the server", async () => {
    const editor = await addCase();
    fireEvent.change(editor, { target: { value: '{\n  "offer": {\n}' } });
    fireEvent.click(screen.getByRole("button", { name: "Add test case" }));
    expect(await screen.findByText(/not valid JSON/)).toBeInTheDocument();
  });

  it("survives a reload, can be edited, and can be removed with undo", async () => {
    await addCase();
    fireEvent.click(screen.getByRole("button", { name: "Add test case" }));
    await screen.findByRole("button", { name: /^My test case/ });
    cleanupAndRender();
    expect(await screen.findByRole("button", { name: /^My test case/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Edit My test case" }));
    const editor = (await screen.findByLabelText("Test case JSON")) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: editor.value.replace("My test case", "Renamed case") } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("button", { name: /^Renamed case/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove Renamed case" }));
    expect(screen.queryByRole("button", { name: /^Renamed case/ })).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("dsa-custom-cases") ?? "[]")).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: /^Renamed case/ })).toBeInTheDocument();
  });

  it("shows the rep's account for a custom case from the rep-safe preview", async () => {
    await addCase();
    fireEvent.click(screen.getByRole("button", { name: "Add test case" }));
    await screen.findByRole("button", { name: /^My test case/ });
    fireEvent.click(screen.getByRole("radio", { name: "Creditor rep" }));
    expect(await screen.findByText("Up to 8 payments")).toBeInTheDocument();
    const repCalls = fetchMock.mock.calls.filter(([u]) => u === "/scenarios/preview/rep");
    expect(repCalls.length).toBeGreaterThan(0);
  });
});

function cleanupAndRender() {
  cleanup();
  render(<App />);
}
