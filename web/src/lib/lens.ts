/**
 * Which side of the call the console shows. User-facing names (Phase 35):
 * `operator` = "Debt negotiator", `creditor` = "Creditor rep" (server `?view=rep`).
 */
import type { View } from "@/types/protocol";

export type Lens = "operator" | "creditor";

export function viewFor(lens: Lens): View {
  return lens === "creditor" ? "rep" : "operator";
}
