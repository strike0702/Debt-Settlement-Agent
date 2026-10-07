/**
 * Custom test cases the visitor adds from the console (Phase 36).
 *
 * A custom case is the pasted JSON (`GET /scenarios/template` shape: meta,
 * offer, firm, client, rep_card) plus what its card shows. It lives only in
 * this browser (`localStorage`, every access wrapped: a private window or
 * blocked storage just means the cases last for this visit). The server never
 * stores it: a call on it sends the JSON as `start.scenario_payload`.
 *
 * `ScenarioSource` is how the rest of the app asks for a scenario's brief or
 * rep account without caring whether it is curated (`GET /scenarios/{id}`) or
 * custom (`POST /scenarios/preview`, `POST /scenarios/preview/rep`).
 */
import type { ScenarioMeta } from "@/types/protocol";

export const CUSTOM_PREFIX = "custom:";
const STORAGE_KEY = "dsa-custom-cases";

export interface CustomCase {
  /** Card id, always `custom:<n>`; never sent to the server. */
  key: string;
  payload: Record<string, unknown>;
  title: string;
  description: string;
  expected: string;
  /** The rep card's suggested replies (from `POST /scenarios/preview/rep`). */
  suggested: string[];
  /** Bumped on every edit so cached briefs for the old JSON are never shown. */
  version: number;
}

export type ScenarioSource =
  | { kind: "catalog"; id: string }
  | { kind: "custom"; key: string; version: number; payload: Record<string, unknown> };

/** A stable cache key for a source (changes when a custom case is edited). */
export function sourceKey(src: ScenarioSource): string {
  return src.kind === "catalog" ? src.id : `${src.key}@${src.version}`;
}

export function isCustomId(id: string): boolean {
  return id.startsWith(CUSTOM_PREFIX);
}

export function customMeta(c: CustomCase): ScenarioMeta & { custom: true } {
  return { id: c.key, title: c.title, description: c.description, expected: c.expected, suggested: c.suggested, custom: true };
}

/** Card text from the case's `meta` block, with plain fallbacks. */
export function metaOf(payload: Record<string, unknown>): { title: string; description: string; expected: string } {
  const meta = (payload.meta ?? {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  return {
    title: str(meta.title) || "Untitled test case",
    description: str(meta.description),
    expected: str(meta.expected) || "deal",
  };
}

/** The id the server sees for this case (`meta.id`, else `custom`). */
export function serverId(payload: Record<string, unknown>): string {
  const meta = (payload.meta ?? {}) as Record<string, unknown>;
  return typeof meta.id === "string" && meta.id.trim() ? meta.id.trim() : "custom";
}

function isCase(v: unknown): v is CustomCase {
  if (!v || typeof v !== "object") return false;
  const c = v as Record<string, unknown>;
  return (
    typeof c.key === "string" &&
    c.key.startsWith(CUSTOM_PREFIX) &&
    !!c.payload &&
    typeof c.payload === "object" &&
    typeof c.title === "string" &&
    Array.isArray(c.suggested)
  );
}

export function loadCases(storage: Pick<Storage, "getItem"> | null = safeStorage()): CustomCase[] {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter(isCase).map((c) => ({ ...c, description: c.description ?? "", expected: c.expected ?? "deal", version: c.version ?? 1 }))
      : [];
  } catch {
    return [];
  }
}

/** Returns false when the browser would not store them (they still work for this visit). */
export function saveCases(cases: CustomCase[], storage: Pick<Storage, "setItem"> | null = safeStorage()): boolean {
  try {
    if (!storage) return false;
    storage.setItem(STORAGE_KEY, JSON.stringify(cases));
    return true;
  } catch {
    return false;
  }
}

function safeStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function nextKey(cases: readonly CustomCase[]): string {
  const used = new Set(cases.map((c) => c.key));
  let n = cases.length + 1;
  while (used.has(`${CUSTOM_PREFIX}${n}`)) n += 1;
  return `${CUSTOM_PREFIX}${n}`;
}

// ---------------------------------------------------------------- JSON editor helpers

export interface FieldError {
  /** Field path as the editor shows it (`client.ledger[2].type`); "" = the whole case. */
  path: string;
  message: string;
}

/** Parse the editor text; a syntax error names its line (and column when the browser says). */
export function parseCase(text: string): { ok: true; payload: Record<string, unknown> } | { ok: false; error: FieldError & { line: number | null } } {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    const line = syntaxErrorLine(text, msg);
    return {
      ok: false,
      error: {
        path: "",
        line,
        message: `This is not valid JSON${line ? ` (line ${line})` : ""}. Check for a missing comma, quote or bracket.`,
      },
    };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, error: { path: "", line: 1, message: "The test case must be a JSON object { … }." } };
  }
  return { ok: true, payload: value as Record<string, unknown> };
}

function syntaxErrorLine(text: string, message: string): number | null {
  const lineCol = /line (\d+)/i.exec(message);
  if (lineCol) return Number(lineCol[1]);
  const pos = /position (\d+)/i.exec(message);
  if (pos) return text.slice(0, Number(pos[1])).split("\n").length;
  return null;
}

/**
 * Character offset of the field a server path names, so an error can move the
 * cursor there. Walks the keys in order (`client` → `ledger` → 3rd `type`);
 * good enough for the template's shape, and `null` when it cannot tell.
 */
export function locatePath(text: string, path: string): number | null {
  if (!path) return null;
  let pos = 0;
  let nth = 0; // occurrences of the next key to skip (from a preceding [i])
  const parts = path.split(".");
  for (let i = 0; i < parts.length; i++) {
    const m = /^([^[]+)((?:\[\d+\])*)$/.exec(parts[i]!);
    if (!m) return null;
    let at = pos;
    for (let k = 0; k <= nth; k++) {
      const found = text.indexOf(`"${m[1]}"`, k === 0 ? at : at + 1);
      if (found < 0) return null;
      at = found;
    }
    pos = at;
    const index = m[2] ? Number(/\[(\d+)\]$/.exec(m[2])![1]) : null;
    nth = 0;
    if (index != null) {
      if (i === parts.length - 1) {
        // The entry itself: the (index+1)-th "{" after the list key.
        let at2 = pos;
        for (let k = 0; k <= index; k++) {
          const found = text.indexOf("{", at2 + 1);
          if (found < 0) return null;
          at2 = found;
        }
        return at2;
      }
      nth = index;
    }
  }
  return pos;
}
