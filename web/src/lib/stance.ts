/**
 * Plain-English line for the creditor rep's stance in the Decision trace
 * (step 1, "Creditor rep said · made an offer"). NLU's `stance` is a code;
 * this is the only place it becomes words. `other` and null show nothing.
 */
import type { TurnTraceEvent } from "@/types/protocol";

export type Stance = NonNullable<TurnTraceEvent["stance"]>;

export const STANCE_TEXT: Record<Stance, string | null> = {
  offer: "made an offer",
  counter: "countered",
  accept: "agreed",
  reject: "pushed back",
  stall: "is stalling",
  info: "gave account details",
  question: "asked a question",
  other: null,
};

/** Colour only where it carries meaning: agreed (good), pushed back (warn). */
export const STANCE_TONE: Partial<Record<Stance, "good" | "warn">> = {
  accept: "good",
  reject: "warn",
};

export function stanceText(stance: Stance | null | undefined): string | null {
  return stance ? STANCE_TEXT[stance] : null;
}
