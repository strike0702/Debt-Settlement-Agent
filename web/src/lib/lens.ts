/** Which side of the call the console shows. `creditor` is the rep's eye (server `?view=rep`). */
import type { View } from "@/types/protocol";

export type Lens = "operator" | "creditor";

export function viewFor(lens: Lens): View {
  return lens === "creditor" ? "rep" : "operator";
}
