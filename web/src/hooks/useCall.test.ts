/**
 * `useCall` over a fake WebSocket: URL and view, start payloads (plain and
 * autoplay), frames reach state and subscribers, `end` hangs up after the
 * close turn, and the last call id survives the end (log download, ported
 * from test_app_js_contracts.py::test_download_log_survives_call_end).
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AUTOPLAY_PAUSE_MS, END_TIMEOUT_MS, useCall, wsUrl } from "@/hooks/useCall";
import type { ServerEvent } from "@/types/protocol";

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  binaryType = "";
  sent: unknown[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(d: unknown) {
    this.sent.push(typeof d === "string" ? JSON.parse(d) : d);
  }
  close() {
    this.closed = true;
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

let n = 0;
const mount = () =>
  renderHook(() =>
    useCall(
      (u) => new FakeSocket(u) as unknown as WebSocket,
      () => `call-${++n}`,
    ),
  );

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe("useCall", () => {
  it("builds the view-scoped URL", () => {
    expect(wsUrl("a b", "rep", { protocol: "https:", host: "x.dev" })).toBe("wss://x.dev/ws/call/a%20b?view=rep");
    expect(wsUrl("id", "operator", { protocol: "http:", host: "localhost:8000" })).toBe(
      "ws://localhost:8000/ws/call/id?view=operator",
    );
  });

  it("starts a live call and an autoplay call with the Phase 22 payloads", () => {
    const hook = mount();
    act(() => hook.result.current.start("easy_deal", { view: "rep" }));
    const ws = FakeSocket.all[0]!;
    expect(ws.url).toMatch(/\/ws\/call\/call-\d+\?view=rep$/);
    act(() => ws.open());
    expect(ws.sent).toEqual([{ type: "start", scenario_id: "easy_deal" }]);
    expect(hook.result.current.status).toBe("live");

    act(() => hook.result.current.start("no_space", { view: "operator", autoplay: true }));
    expect(ws.closed).toBe(true);
    const ws2 = FakeSocket.all[1]!;
    act(() => ws2.open());
    expect(ws2.sent).toEqual([
      { type: "start", scenario_id: "no_space", autoplay: true, autoplay_pause_ms: AUTOPLAY_PAUSE_MS },
    ]);
    expect(hook.result.current.autoplay).toBe(true);
  });

  it("collects frames, notifies subscribers, and ignores a replaced socket", () => {
    const hook = mount();
    const seen: string[] = [];
    act(() => void hook.result.current.subscribe((ev) => seen.push(ev.type)));
    act(() => hook.result.current.start("easy_deal", { view: "operator" }));
    const old = FakeSocket.all[0]!;
    act(() => old.open());
    act(() => old.emit({ type: "say", id: "s1", text: "Hi." }));
    act(() => hook.result.current.start("easy_deal", { view: "operator" }));
    act(() => old.emit({ type: "say", id: "stale", text: "x" }));
    expect(seen).toEqual(["say"]);
    expect(hook.result.current.events).toEqual([]);
  });

  it("sends text and WAV only while open", () => {
    const hook = mount();
    expect(hook.result.current.sendText("hi")).toBe(false);
    act(() => hook.result.current.start("easy_deal", { view: "operator" }));
    const ws = FakeSocket.all[0]!;
    act(() => ws.open());
    expect(hook.result.current.sendText("Forty percent.", "suggested")).toBe(true);
    expect(hook.result.current.sendWav(new ArrayBuffer(4))).toBe(true);
    expect(ws.sent.slice(1)).toEqual([{ type: "text", text: "Forty percent.", source: "suggested" }, new ArrayBuffer(4)]);
  });

  it("end: hangs up after the close turn; the last call id stays downloadable", () => {
    const hook = mount();
    act(() => hook.result.current.start("easy_deal", { view: "rep" }));
    const ws = FakeSocket.all[0]!;
    act(() => ws.open());
    const id = hook.result.current.callId;
    act(() => hook.result.current.end());
    expect(ws.sent.at(-1)).toEqual({ type: "end" });
    expect(hook.result.current.status).toBe("ending");
    act(() => ws.emit({ type: "phase", phase: "END", intent: "CLOSE", turn: 4 }));
    act(() => ws.emit({ type: "turn_done" }));
    expect(ws.closed).toBe(true);
    expect(hook.result.current.status).toBe("closed");
    expect(hook.result.current.lastCallId).toBe(id);
  });

  it("end: closes after a timeout if the server never answers", () => {
    const hook = mount();
    act(() => hook.result.current.start("easy_deal", { view: "rep" }));
    const ws = FakeSocket.all[0]!;
    act(() => ws.open());
    act(() => hook.result.current.end());
    act(() => vi.advanceTimersByTime(END_TIMEOUT_MS));
    expect(ws.closed).toBe(true);
    expect(hook.result.current.status).toBe("closed");
    expect(hook.result.current.lastCallId).not.toBeNull();
  });
});
