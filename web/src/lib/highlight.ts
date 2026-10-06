/**
 * Text splitting for the decision trace: verified NLU quotes inside the rep
 * line, and `{placeholder}` slots inside an NLG template. Pure string work.
 */

export interface Segment {
  text: string;
  hit: boolean;
}

/** Split `text` so every case-insensitive occurrence of a quote is a `hit` segment. */
export function splitByQuotes(text: string, quotes: readonly string[]): Segment[] {
  const lower = text.toLowerCase();
  const spans: [number, number][] = [];
  for (const q of quotes) {
    const needle = q.trim().toLowerCase();
    if (!needle) continue;
    const at = lower.indexOf(needle);
    if (at >= 0) spans.push([at, at + needle.length]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  // Merge overlaps so nested quotes render as one mark.
  const merged: [number, number][] = [];
  for (const s of spans) {
    const last = merged.at(-1);
    if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1]);
    else merged.push([s[0], s[1]]);
  }
  const out: Segment[] = [];
  let pos = 0;
  for (const [a, b] of merged) {
    if (a > pos) out.push({ text: text.slice(pos, a), hit: false });
    out.push({ text: text.slice(a, b), hit: true });
    pos = b;
  }
  if (pos < text.length) out.push({ text: text.slice(pos), hit: false });
  return out;
}

/** Split an NLG template into literal text and `{placeholder}` segments (hit = placeholder). */
export function splitTemplate(template: string): Segment[] {
  return template
    .split(/(\{[a-z_]+\})/g)
    .filter((p) => p !== "")
    .map((p) => ({ text: p, hit: /^\{[a-z_]+\}$/.test(p) }));
}
