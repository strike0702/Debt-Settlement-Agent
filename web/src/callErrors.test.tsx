/**
 * Call errors in the console: `error` frames become one
 * plain, sentence-case alert next to the composer (newest only), with the raw
 * server text in the Debt negotiator view only. The alert clears on the next
 * agent reply or a new call, and the composer stays usable.
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { Conversation } from "@/components/Conversation";
import { callErrorText, GENERIC_ERROR } from "@/lib/callError";
import { foldCall, reduceCall } from "@/lib/callState";
import type { RepAccount, ScenarioMeta, ServerEvent } from "@/types/protocol";

const NLU_DOWN = "NLU unavailable: all providers failed (groq: 429)";
const READ_FAIL = "The agent could not read that line. Try sending it again.";

describe("callErrorText", () => {
  it("maps known server messages to plain copy and anything else to a generic line", () => {
    expect(callErrorText({ kind: "error", message: NLU_DOWN })).toBe(READ_FAIL);
    expect(callErrorText({ kind: "error", message: "call not started" })).toBe("The call has not started yet. Press Start call first.");
    expect(callErrorText({ kind: "error", message: "autoplay is driving this call" })).toBe(
      "The simulated rep is playing this call, so your reply was not sent.",
    );
    expect(callErrorText({ kind: "error", message: "unknown event: zap" })).toBe(GENERIC_ERROR);
  });
});

const err = (message: string): ServerEvent => ({ type: "error", message });
const creditor = (text: string): ServerEvent => ({ type: "transcript", role: "creditor", text, spoken: false, sentence_id: null, blocked: false });

describe("call state", () => {
  it("keeps the newest error only, stops thinking, and clears on the agent's next reply", () => {
    let s = foldCall([creditor("Fifty percent."), err("call not started"), err(NLU_DOWN)]);
    expect(s.thinking).toBe(false);
    expect(s.error).toEqual({ kind: "error", message: NLU_DOWN, seq: 2 });
    // The rep's next line does not clear it; the agent's reply does.
    s = reduceCall(s, creditor("Fifty percent."));
    expect(s.error?.seq).toBe(2);
    expect(reduceCall(s, { type: "say", id: "s1", text: "Thanks." }).error).toBeNull();
    const agent: ServerEvent = { type: "transcript", role: "agent", text: "Thanks.", spoken: false, sentence_id: "s1", blocked: false };
    expect(reduceCall(s, agent).error).toBeNull();
    // A later error after a clear still gets a new seq.
    expect(reduceCall(reduceCall(s, agent), err(NLU_DOWN)).error?.seq).toBe(3);
  });

  it("starts a new call with none", () => {
    expect(foldCall([]).error).toBeNull();
  });
});

describe("Conversation error alert", () => {
  const base = { messages: [], suggested: [], emptyHint: "" };

  it("announces the plain line, with the server text only when given", () => {
    const { rerender } = render(<Conversation {...base} onSend={() => {}} error={{ text: READ_FAIL, detail: NLU_DOWN, seq: 1 }} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(READ_FAIL);
    expect(alert).toHaveTextContent(NLU_DOWN);
    rerender(<Conversation {...base} onSend={() => {}} error={{ text: READ_FAIL, detail: null, seq: 1 }} />);
    expect(screen.getByRole("alert")).not.toHaveTextContent("NLU unavailable");
    rerender(<Conversation {...base} onSend={() => {}} error={null} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("puts the failed line back in the text box so it can be sent again", () => {
    const sent: string[] = [];
    const { rerender } = render(<Conversation {...base} onSend={(t) => sent.push(t)} />);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "We want fifty percent." } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(box).toHaveValue("");
    rerender(<Conversation {...base} onSend={(t) => sent.push(t)} error={{ text: READ_FAIL, seq: 1 }} />);
    expect(box).toHaveValue("We want fifty percent.");
    expect(box).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(sent).toEqual(["We want fifty percent.", "We want fifty percent."]);
  });
});

// ---------------------------------------------------------------- live App

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
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

const SCENARIOS: ScenarioMeta[] = [{ id: "easy_deal", title: "Easy deal", description: "d", expected: "deal", suggested: [] }];
const REP: RepAccount = {
  id: "easy_deal",
  creditor: { name: "NorthPeak Collections", outstanding_balance_cents: 125000, original_balance_cents: 160000 },
  rules: { max_payments: 8, min_payment_cents: 10000, structure: "even", opening_ask_bp: 4500, floor_bp: 4000, first_payment: null },
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

beforeEach(() => {
  FakeSocket.all = [];
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/scenarios") return json(SCENARIOS);
      if (url === "/scenarios/easy_deal/rep") return json(REP);
      return new Response("null", { status: 404 });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function startLiveCall(): Promise<FakeSocket> {
  fireEvent.click(await screen.findByRole("button", { name: /Start call/ }));
  const ws = FakeSocket.all.at(-1)!;
  act(() => ws.open());
  return ws;
}

describe("live console", () => {
  it("shows the plain line and the server text in the Debt negotiator view, then clears on the reply", async () => {
    render(<App />);
    const ws = await startLiveCall();
    act(() => ws.emit(err(NLU_DOWN)));
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(READ_FAIL);
    expect(within(alert).getByText(/Server message/)).toHaveTextContent(NLU_DOWN);
    // The composer still works after the error.
    expect(screen.getByRole("textbox")).toBeEnabled();
    act(() => ws.emit({ type: "say", id: "s1", text: "Thanks." }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows only the plain line in the Creditor rep view", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("radio", { name: "Creditor rep" }));
    const ws = await startLiveCall();
    act(() => ws.emit(err(NLU_DOWN)));
    expect(screen.getByRole("alert")).toHaveTextContent(READ_FAIL);
    expect(document.body.textContent).not.toContain("NLU unavailable");
    expect(document.body.textContent).not.toContain("groq");
  });

  it("starts a new call with no error showing", async () => {
    render(<App />);
    const ws = await startLiveCall();
    act(() => ws.emit(err(NLU_DOWN)));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /End call/ }));
    act(() => ws.onclose?.());
    await startLiveCall();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
