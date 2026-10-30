/**
 * React wrapper around `WebRtcVoice` (lib/voice/webrtc.ts), the Pipecat voice client.
 *
 * Owns one client per mount and exposes its snapshot and the panel's call
 * state. The agent plays through one `<audio>` element made here (not in the
 * DOM; Chrome plays a detached element's `srcObject`). App feeds
 * every socket frame into `engine.onServerEvent` and routes failures to the
 * visible error line through `io.onError`. Tests pass fake `deps`.
 */
import { useEffect, useLayoutEffect, useState, useSyncExternalStore } from "react";
import {
  callVoiceState,
  WebRtcVoice,
  type CallVoiceState,
  type RtcDeps,
  type RtcIO,
  type RtcSnapshot,
  type IceServerLike,
} from "@/lib/voice/webrtc";

/** The real browser: getUserMedia, RTCPeerConnection, fetch, an `<audio>` element. */
export function browserRtcDeps(): RtcDeps {
  let audio: HTMLAudioElement | null = null;
  return {
    getUserMedia: (c) => navigator.mediaDevices.getUserMedia(c),
    makePeer: (iceServers) =>
      new RTCPeerConnection({
        iceServers,
        // `?ice=relay` forces the TURN relay (testing what Render needs).
        iceTransportPolicy: new URLSearchParams(location.search).get("ice") === "relay" ? "relay" : "all",
      }) as unknown as ReturnType<RtcDeps["makePeer"]>,
    iceServers: async (callId) => {
      const res = await fetch(`/voice/ice?call_id=${encodeURIComponent(callId)}`);
      if (!res.ok) throw new Error(`voice ice ${res.status}`);
      return ((await res.json()) as { iceServers: IceServerLike[] }).iceServers;
    },
    fetch: (url, init) => fetch(url, init),
    playRemote: (stream, track) => {
      audio ??= Object.assign(new Audio(), { autoplay: true });
      const el = audio;
      el.srcObject = stream ?? new MediaStream([track]);
      void el.play().catch(() => undefined);
    },
    now: () => performance.now(),
    log: (msg, data) => console.info(msg, data),
    onDeviceChange: (fn) => {
      navigator.mediaDevices.addEventListener("devicechange", fn);
      return () => navigator.mediaDevices.removeEventListener("devicechange", fn);
    },
  };
}

export interface UseWebRtcVoice {
  snapshot: RtcSnapshot;
  state: CallVoiceState;
  engine: WebRtcVoice;
}

export function useWebRtcVoice(io: RtcIO, deps?: RtcDeps): UseWebRtcVoice {
  // One client per mount; `deps` is read once.
  const [engine] = useState(() => new WebRtcVoice(io, deps ?? browserRtcDeps()));
  useLayoutEffect(() => engine.setIO(io));
  const snapshot = useSyncExternalStore(engine.subscribe, engine.snapshot);
  useEffect(() => () => engine.dispose(), [engine]);
  return { snapshot, state: callVoiceState(snapshot), engine };
}
