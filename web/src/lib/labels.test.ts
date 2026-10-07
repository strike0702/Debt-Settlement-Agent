/** Every code-to-words label is sentence case (Phase 36). */
import { describe, expect, it } from "vitest";
import { EXPECTED_LABEL, expectedLabel, sentence, STATUS_LABEL } from "@/lib/labels";

const isSentence = (s: string) => /^[A-Z0-9$]/.test(s) && !/^[A-Z_]+$/.test(s);

describe("labels", () => {
  it("are sentence case, never raw codes", () => {
    for (const s of [...Object.values(STATUS_LABEL), ...Object.values(EXPECTED_LABEL)]) {
      expect(isSentence(s), s).toBe(true);
    }
  });

  it("word unknown codes", () => {
    expect(expectedLabel("partial_deal")).toBe("Partial deal");
    expect(sentence("pending client approval")).toBe("Pending client approval");
  });
});
