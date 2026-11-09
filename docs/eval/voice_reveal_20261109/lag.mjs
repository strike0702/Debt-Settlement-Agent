/**
 * Median lag between the text's last word and the sentence's end event, from a
 * harness run's raw.jsonl (page receive times of agent_sentence_start / _end) and
 * the app's own pace table. Positive: the last word showed before the audio ended.
 *
 *   npx esbuild web/src/lib/voice/reveal.ts --format=esm --outfile=/tmp/reveal.mjs
 *   node docs/eval/voice_reveal_20261109/lag.mjs /tmp/reveal.mjs <raw.jsonl>
 */
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const { wordTimes } = await import(pathToFileURL(process.argv[2]).href);
const rows = readFileSync(process.argv[3], "utf8").trim().split("\n").map((l) => JSON.parse(l));
const page = rows.filter((r) => r.src === "page").sort((a, b) => a.t - b.t);
const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b);
  return s.length ? s[Math.floor(s.length / 2)] : null;
};
const out = [];
let open = null;
for (const r of page) {
  if (r.kind !== "ws_in") continue;
  if (r.type === "agent_sentence_start") open = r;
  else if (r.type === "agent_sentence_cut") open = null;
  else if (r.type === "agent_sentence_end" && open) {
    const est = wordTimes(open.text).at(-1);
    out.push({ text: open.text, real_ms: Math.round(r.t - open.t), est_ms: est, lag_ms: Math.round(r.t - open.t - est) });
    open = null;
  }
}
for (const o of out) console.log(String(o.lag_ms).padStart(6), String(o.real_ms).padStart(6), String(o.est_ms).padStart(6), o.text);
const lags = out.map((o) => o.lag_ms);
console.log(`sentences ${lags.length}; median lag ${median(lags)} ms; median abs ${median(lags.map(Math.abs))} ms; max abs ${Math.max(...lags.map(Math.abs))} ms`);
