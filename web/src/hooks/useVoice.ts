/**
 * React wrapper around `VoiceEngine` (lib/voice/engine.ts): mic, STT, TTS, barge-in.
 *
 * Owns one engine per mount, exposes its snapshot, and maps it to the
 * `MicState` the conversation panel shows. The engine never touches the socket
 * directly: `io` (from App, backed by `useCall`) sends frames, and App feeds
 * every server event into `onServerEvent`. Tests pass fake `deps`.
 */
import { useEffect, useLayoutEffect, useState, useSyncExternalStore } from "react";
import type { MicState } from "@/lib/mic";
import { VoiceEngine, type SttMode, type VoiceDeps, type VoiceIO, type VoiceSnapshot } from "@/lib/voice/engine";
import { createMicVad } from "@/lib/voice/vad";

/** The real browser: speechSynthesis, getUserMedia, vad-web from the CDN, webkitSpeechRecognition. */
export function browserVoiceDeps(): VoiceDeps {
  const w = window as unknown as {
    speechSynthesis?: VoiceDeps["synth"];
    SpeechRecognition?: VoiceDeps["SpeechRecognition"];
    webkitSpeechRecognition?: VoiceDeps["SpeechRecognition"];
  };
  return {
    now: () => performance.now(),
    synth: w.speechSynthesis ?? null,
    makeUtterance: (text) => new SpeechSynthesisUtterance(text) as unknown as ReturnType<VoiceDeps["makeUtterance"]>,
    getUserMedia: (c) => navigator.mediaDevices.getUserMedia(c),
    createVad: createMicVad,
    SpeechRecognition: w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null,
  };
}

/** Mic indicator: off, then speaking > thinking > listening. */
export function micStateOf(s: VoiceSnapshot): MicState {
  if (!s.micOn) return "off";
  if (s.speaking) return "speaking";
  if (s.waiting) return "thinking";
  return "listening";
}

export interface UseVoice {
  snapshot: VoiceSnapshot;
  mic: MicState;
  engine: VoiceEngine;
  toggleMic: () => void;
  setSttMode: (m: SttMode) => void;
}

export function useVoice(io: VoiceIO, deps?: VoiceDeps, sttMode: SttMode = "server"): UseVoice {
  // One engine per mount; `deps` and the initial mode are read once.
  const [engine] = useState(() => new VoiceEngine(io, deps ?? browserVoiceDeps(), sttMode));
  // `io` closes over fresh call state each render; hand the engine the latest.
  useLayoutEffect(() => engine.setIO(io));
  const snapshot = useSyncExternalStore(engine.subscribe, engine.snapshot);

  useEffect(() => {
    const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
    const onVoices = () => engine.voicesChanged();
    synth?.addEventListener?.("voiceschanged", onVoices);
    return () => {
      synth?.removeEventListener?.("voiceschanged", onVoices);
      engine.dispose();
    };
  }, [engine]);

  return {
    snapshot,
    mic: micStateOf(snapshot),
    engine,
    toggleMic: () => void engine.toggleMic(),
    setSttMode: (m) => void engine.setSttMode(m),
  };
}
