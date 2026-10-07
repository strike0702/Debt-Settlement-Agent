import { describe, expect, it } from "vitest";
import { STANCE_TEXT, STANCE_TONE, stanceText } from "@/lib/stance";

describe("stanceText (Phase 35)", () => {
  it("maps every NLU stance to plain English, hiding other and null", () => {
    expect(STANCE_TEXT).toEqual({
      offer: "made an offer",
      counter: "countered",
      accept: "agreed",
      reject: "pushed back",
      stall: "is stalling",
      info: "gave account details",
      question: "asked a question",
      other: null,
    });
    expect(stanceText("other")).toBeNull();
    expect(stanceText(null)).toBeNull();
    expect(stanceText(undefined)).toBeNull();
    expect(stanceText("reject")).toBe("pushed back");
  });

  it("colours only agreed (good) and pushed back (warn)", () => {
    expect(STANCE_TONE).toEqual({ accept: "good", reject: "warn" });
  });
});
