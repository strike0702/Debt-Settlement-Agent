/** Helpers to fold the whole recorded fixture through a lens, for component tests. */
import { eventsForLens } from "@/App";
import { easyDeal } from "@/fixtures";
import { foldCall, type CallState } from "@/lib/callState";
import type { Lens } from "@/lib/lens";

export function fullCall(lens: Lens): CallState {
  return foldCall(eventsForLens(easyDeal.frames.map((f) => f.ev), lens));
}
