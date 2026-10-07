/**
 * LiveApp scenario picker over mocked `fetch` and `WebSocket`: the picker is
 * locked while a call runs and unlocks once the call is finished, even though
 * a finished autoplay call leaves its socket open (Phase 23b fix).
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

const SCENARIOS: ScenarioMeta[] = [
  { id: "easy_deal", title: "Easy deal", description: "d", expected: "deal", suggested: [] },
  { id: "no_space", title: "No space", description: "d", expected: "no_deal", suggested: [] },
];

beforeEach(() => {
  FakeSocket.all = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/scenarios"
        ? new Response(JSON.stringify(SCENARIOS), { status: 200 })
        : new Response("null", { status: 404 }),
    ),
  );
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
