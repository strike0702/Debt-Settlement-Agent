/**
 * One live call over `/ws/call/{id}?view=rep|operator`.
 *
 * Opens a socket per call (the view is fixed when it connects), sends `start`
 * (plain or autoplay), and collects every frame. It does not fold state or
 * filter privacy: App folds `events` with `reduceCall` after `eventsForLens`,
 * and the rep view is filtered by the server. Raw frames also go to
 * `subscribe` listeners (the voice engine). `lastCallId` survives the end of
 * a call so its audit log stays downloadable.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { CallEvent } from "@/lib/callState";
import type { ClientEvent, ServerEvent, View } from "@/types/protocol";

export type CallStatus = "idle" | "connecting" | "live" | "ending" | "closed";

/** Close the socket if the server has not finished the call this long after `end`. */
export const END_TIMEOUT_MS = 3000;
export const AUTOPLAY_PAUSE_MS = 1200;

export interface StartOptions {
  view: View;
  autoplay?: boolean;
}

export interface UseCall {
  events: CallEvent[];
  status: CallStatus;
  callId: string | null;
  /** The last call that was started; kept after it ends for the log download. */
  lastCallId: string | null;
  view: View | null;
  autoplay: boolean;
  start: (scenarioId: string, opts: StartOptions) => void;
  end: () => void;
  sendText: (text: string, source?: string) => boolean;
  sendJson: (ev: ClientEvent) => void;
  sendWav: (wav: ArrayBuffer) => boolean;
  /** Append a client-side event (`tts_onset`) to the stream. */
  addLocal: (ev: CallEvent) => void;
  subscribe: (fn: (ev: ServerEvent) => void) => () => void;
}

export function wsUrl(callId: string, view: View, loc: Pick<Location, "protocol" | "host"> = window.location): string {
  const proto = loc.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${loc.host}/ws/call/${encodeURIComponent(callId)}?view=${view}`;
}

export function useCall(
  makeSocket: (url: string) => WebSocket = (u) => new WebSocket(u),
  makeId: () => string = () => crypto.randomUUID(),
): UseCall {
  const [events, setEvents] = useState<CallEvent[]>([]);
  const [status, setStatus] = useState<CallStatus>("idle");
  const [callId, setCallId] = useState<string | null>(null);
  const [lastCallId, setLastCallId] = useState<string | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [autoplay, setAutoplay] = useState(false);
  const sock = useRef<WebSocket | null>(null);
  const endTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listeners = useRef(new Set<(ev: ServerEvent) => void>());
  // After `end`, the server's close turn arrives as phase END … turn_done; then we hang up.
  const ending = useRef(false);
  const sawEnd = useRef(false);

  const close = useCallback(() => {
    if (endTimer.current) clearTimeout(endTimer.current);
    endTimer.current = null;
    const old = sock.current;
    sock.current = null;
    if (!old) return;
    old.onopen = old.onmessage = old.onclose = old.onerror = null;
    try {
      old.close();
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => close, [close]);

  const start = useCallback(
    (scenarioId: string, opts: StartOptions) => {
      close();
      const id = makeId();
      const ws = makeSocket(wsUrl(id, opts.view));
      sock.current = ws;
      ws.binaryType = "arraybuffer";
      ending.current = false;
      sawEnd.current = false;
      setEvents([]);
      setStatus("connecting");
      setCallId(id);
      setLastCallId(id);
      setView(opts.view);
      setAutoplay(Boolean(opts.autoplay));
      // Handlers check identity so a late frame from a replaced socket cannot touch the new call.
      ws.onopen = () => {
        if (sock.current !== ws) return;
        const msg: ClientEvent = opts.autoplay
          ? { type: "start", scenario_id: scenarioId, autoplay: true, autoplay_pause_ms: AUTOPLAY_PAUSE_MS }
          : { type: "start", scenario_id: scenarioId };
        ws.send(JSON.stringify(msg));
        setStatus("live");
      };
      ws.onmessage = (m: MessageEvent) => {
        if (sock.current !== ws || typeof m.data !== "string") return;
        let ev: ServerEvent;
        try {
          ev = JSON.parse(m.data) as ServerEvent;
        } catch {
          return;
        }
        setEvents((prev) => [...prev, ev]);
        listeners.current.forEach((fn) => fn(ev));
        if (ev.type === "phase" && ev.phase === "END") sawEnd.current = true;
        if (ending.current && (ev.type === "error" || (ev.type === "turn_done" && sawEnd.current))) {
          close();
          setStatus("closed");
        }
      };
      ws.onclose = () => {
        if (sock.current !== ws) return;
        sock.current = null;
        if (endTimer.current) clearTimeout(endTimer.current);
        endTimer.current = null;
        setStatus("closed");
      };
    },
    [close, makeId, makeSocket],
  );

  const isOpen = () => sock.current?.readyState === WebSocket.OPEN;

  const sendJson = useCallback((ev: ClientEvent) => {
    if (sock.current?.readyState === WebSocket.OPEN) sock.current.send(JSON.stringify(ev));
  }, []);

  const end = useCallback(() => {
    if (!isOpen()) {
      close();
      setStatus("closed");
      return;
    }
    ending.current = true;
    setStatus("ending");
    sendJson({ type: "end" });
    // Never leave the call stuck if the server does not answer the end.
    if (endTimer.current) clearTimeout(endTimer.current);
    endTimer.current = setTimeout(() => {
      close();
      setStatus("closed");
    }, END_TIMEOUT_MS);
  }, [close, sendJson]);

  const sendText = useCallback(
    (text: string, source?: string) => {
      if (!isOpen()) return false;
      sendJson(source ? { type: "text", text, source } : { type: "text", text });
      return true;
    },
    [sendJson],
  );

  const sendWav = useCallback((wav: ArrayBuffer) => {
    if (!isOpen()) return false;
    sock.current!.send(wav);
    return true;
  }, []);

  const addLocal = useCallback((ev: CallEvent) => setEvents((prev) => [...prev, ev]), []);

  const subscribe = useCallback((fn: (ev: ServerEvent) => void) => {
    listeners.current.add(fn);
    return () => {
      listeners.current.delete(fn);
    };
  }, []);

  return { events, status, callId, lastCallId, view, autoplay, start, end, sendText, sendJson, sendWav, addLocal, subscribe };
}
