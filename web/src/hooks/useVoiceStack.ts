/**
 * Which voice client this server wants: `GET /voice/config` → `legacy` or `pipecat`.
 *
 * `legacy` (browser TTS, VAD and STT; `useVoice`) until the answer arrives or
 * when the request fails, so an older server keeps the old client. `pipecat`
 * selects the WebRTC client (`useWebRtcVoice`).
 */
import { useEffect, useState } from "react";

export type VoiceStack = "legacy" | "pipecat";

const loadConfig = () => fetch("/voice/config");

export function useVoiceStack(load: () => Promise<Response> = loadConfig): VoiceStack {
  const [stack, setStack] = useState<VoiceStack>("legacy");
  useEffect(() => {
    let live = true;
    load()
      .then((r) => (r.ok ? r.json() : null))
      .then((body: { stack?: string } | null) => {
        if (live && body?.stack === "pipecat") setStack("pipecat");
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [load]);
  return stack;
}
