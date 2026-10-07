/** Every code-to-words label is sentence case (Phase 36). */
import { describe, expect, it } from "vitest";
import { EXPECTED_LABEL, guardLabel, INTENT_LABEL, intentLabel, sentence, STATUS_LABEL } from "@/lib/labels";
import { STANCE_TEXT } from "@/lib/stance";

const isSentence = (s: string) => /^[A-Z0-9$]/.test(s) && !/^[A-Z_]+$/.test(s);

describe("labels", () => {
  it("are sentence case, never raw codes", () => {
    for (const s of [...Object.values(INTENT_LABEL), ...Object.values(STATUS_LABEL), ...Object.values(EXPECTED_LABEL)]) {
      expect(isSentence(s), s).toBe(true);
    }
    for (const s of Object.values(STANCE_TEXT)) if (s) expect(isSentence(sentence(s)), s).toBe(true);
  });

  it("word guard verdicts and unknown codes", () => {
    expect(guardLabel("rendered", false, "number_not_from_facts")).toBe("Final line check blocked: number not from facts");
    expect(guardLabel("template", true, null)).toBe("Template check passed");
    expect(intentLabel("SOMETHING_NEW")).toBe("Something new");
  });
});
