/**
 * Backfills the Debt negotiator view for a call that streams the Creditor rep
 * view (Phase 36). The rep socket never carries `turn_trace` or private audit
 * rows, so the server keeps them per call in memory and serves them at
 * `GET /calls/{id}/operator`; this hook fetches that while the negotiator view
 * is open, and again whenever `tick` changes (App passes the number of
 * `turn_done` frames, so the trace stays current turn by turn).
 *
 * It does not merge anything: `withOperatorDetail` in `lib/callState.ts` does.
 * `gone` means the server no longer has the call (restarted, or evicted).
 */
import { useEffect, useState } from "react";
import type { OperatorDetail } from "@/types/protocol";

export type OperatorDetailState =
  | { status: "off" }
  | { status: "loading" }
  | { status: "ready"; detail: OperatorDetail }
  | { status: "gone" };

export function useOperatorDetail(callId: string | null, tick: number): OperatorDetailState {
  const [loaded, setLoaded] = useState<{ callId: string; state: OperatorDetailState } | null>(null);
  useEffect(() => {
    if (!callId) return;
    let live = true;
    fetch(`/calls/${encodeURIComponent(callId)}/operator`)
      .then((r) => (r.ok ? (r.json() as Promise<OperatorDetail>) : null))
      .then((detail) => live && setLoaded({ callId, state: detail ? { status: "ready", detail } : { status: "gone" } }))
      .catch(() => live && setLoaded({ callId, state: { status: "gone" } }));
    return () => {
      live = false;
    };
  }, [callId, tick]);
  if (!callId) return { status: "off" };
  // Keep showing the last snapshot of this call while the next one loads.
  return loaded?.callId === callId ? loaded.state : { status: "loading" };
}
