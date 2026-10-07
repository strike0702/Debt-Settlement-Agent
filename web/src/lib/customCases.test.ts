/** Custom test-case helpers (Phase 36): storage that never throws, JSON errors with lines, path → cursor. */
import { describe, expect, it } from "vitest";
import { type CustomCase, loadCases, locatePath, metaOf, nextKey, parseCase, saveCases, serverId } from "@/lib/customCases";

const TEXT = JSON.stringify(
  {
    meta: { id: "custom", title: "T" },
    client: { draft_day: 15, ledger: [{ date: "a", type: "credit" }, { date: "b", type: "credit" }, { date: "c", type: "debit" }] },
  },
  null,
  2,
);

const CASE: CustomCase = { key: "custom:1", payload: { meta: {} }, title: "T", description: "", expected: "deal", suggested: [], version: 1 };

describe("custom case storage", () => {
  it("round-trips through storage and drops malformed entries", () => {
    const mem = new Map<string, string>();
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) };
    expect(saveCases([CASE], store)).toBe(true);
    mem.set("dsa-custom-cases", JSON.stringify([CASE, { key: "evil", payload: {} }, 3]));
    expect(loadCases(store)).toEqual([CASE]);
  });

  it("never throws when storage is blocked or corrupt", () => {
    const blocked = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    };
    expect(loadCases(blocked)).toEqual([]);
    expect(saveCases([CASE], blocked)).toBe(false);
    expect(loadCases({ getItem: () => "{not json" })).toEqual([]);
    expect(loadCases(null)).toEqual([]);
  });

  it("picks a fresh key and reads card text from meta", () => {
    expect(nextKey([CASE, { ...CASE, key: "custom:2" }])).toBe("custom:3");
    expect(metaOf({})).toEqual({ title: "Untitled test case", description: "", expected: "deal" });
    expect(serverId({ meta: { id: " mine " } })).toBe("mine");
    expect(serverId({})).toBe("custom");
  });
});

describe("parseCase", () => {
  it("names the line of a syntax error", () => {
    const r = parseCase('{\n  "a": 1,\n  "b": \n}');
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error.message).toMatch(/^This is not valid JSON/);
  });

  it("rejects a non-object", () => {
    const r = parseCase("[1]");
    expect(r.ok).toBe(false);
  });
});

describe("locatePath", () => {
  it("finds nested keys and the n-th list entry's key", () => {
    expect(TEXT.slice(locatePath(TEXT, "client.draft_day")!)).toMatch(/^"draft_day"/);
    const third = locatePath(TEXT, "client.ledger[2].type")!;
    expect(TEXT.slice(third, third + 30)).toContain('"debit"');
    const entry = locatePath(TEXT, "client.ledger[1]")!;
    expect(TEXT.slice(entry, entry + 40)).toContain('"b"');
    expect(locatePath(TEXT, "")).toBeNull();
    expect(locatePath(TEXT, "nope.x")).toBeNull();
  });
});
