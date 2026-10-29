/**
 * LiveApp on the Pipecat stack (`/voice/config` says `pipecat`): the WebRTC
 * voice controls replace the legacy mic, `say` lines are never spoken in the
 * browser, and a budget refusal on the socket shows on the visible error line.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "@/App";
import type { ScenarioMeta, ServerEvent } from "@/types/protocol";

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
  send(): void {}
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
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const speak = vi.fn();

beforeEach(() => {
  FakeSocket.all = [];
  speak.mockClear();
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("speechSynthesis", { speak, cancel: vi.fn(), resume: vi.fn(), getVoices: () => [] });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/voice/config") return json({ stack: "pipecat" });
      if (url === "/scenarios") return json(SCENARIOS);
      return json(null, 404);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

it("uses the WebRTC controls, never speaks say lines, and shows a budget refusal", async () => {
  render(<App />);
  expect(await screen.findByRole("button", { name: "Turn voice on" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /Turn mic on/ })).toBeNull();
  expect(screen.getByTestId("voice-state")).toHaveTextContent("Voice off");

  fireEvent.click(await screen.findByRole("button", { name: /Start call/ }));
  const ws = FakeSocket.all.at(-1)!;
  act(() => ws.open());
  act(() => ws.emit({ type: "say", id: "s1", text: "Hello, this is the agent." }));
  expect(speak).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Turn voice on" })).toBeEnabled();

  act(() => ws.emit({ type: "error", message: "Voice is paused: today's voice budget is used up. Text chat still works." }));
  expect(screen.getByTestId("call-error")).toHaveTextContent("Voice is paused for today because the voice budget is used up.");
});
