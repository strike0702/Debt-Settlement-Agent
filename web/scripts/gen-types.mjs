/**
 * Generate src/types/events.ts from src/types/events.schema.json.
 *
 * The schema is exported from the Pydantic models in app/schemas/events.py
 * (`python -m app.schemas.events --out web/src/types/events.schema.json`), so
 * the WS protocol has one source of truth. `npm run gen:types` rewrites the
 * file; CI regenerates it and fails on a diff.
 *
 * Pre-processing: property-level `title`s are dropped (otherwise every field
 * becomes its own alias such as `MaxBp`), and the three root properties
 * (`ServerEvent`, `ClientEvent`, `View`) are lifted into named definitions.
 */
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const SCHEMA = here("../src/types/events.schema.json");
const OUT = here("../src/types/events.ts");

const SHAPE_KEYS = ["type", "$ref", "anyOf", "oneOf", "allOf", "enum", "const", "properties", "items"];

/**
 * Drop `title` everywhere except on top-level definitions, drop the OpenAPI-only
 * `discriminator`, and type shapeless schemas (Pydantic `Any`) as `unknown`
 * instead of json2ts's default open object.
 */
function strip(node, keepTitle) {
  if (Array.isArray(node)) return node.map((n) => strip(n, false));
  if (!node || typeof node !== "object") return node;
  const out = {};
  if (!SHAPE_KEYS.some((k) => k in node)) out.tsType = "unknown";
  for (const [k, v] of Object.entries(node)) {
    if (k === "title" && !keepTitle) continue;
    if (k === "discriminator") continue;
    if (k === "$defs" || k === "properties") {
      out[k] = Object.fromEntries(Object.entries(v).map(([name, sub]) => [name, strip(sub, k === "$defs")]));
    } else {
      out[k] = strip(v, false);
    }
  }
  return out;
}

const raw = JSON.parse(await readFile(SCHEMA, "utf8"));
const defs = { ...raw.$defs };
for (const name of ["ServerEvent", "ClientEvent", "View"]) {
  defs[name] = { ...raw.properties[name], title: name };
}
const schema = strip(
  {
    title: "WsProtocol",
    type: "object",
    additionalProperties: false,
    properties: Object.fromEntries(
      ["ServerEvent", "ClientEvent", "View"].map((n) => [n, { $ref: `#/$defs/${n}` }]),
    ),
    required: ["ServerEvent", "ClientEvent", "View"],
    $defs: defs,
  },
  true,
);

const banner = `/**
 * WebSocket protocol for \`/ws/call/{call_id}?view=rep|operator\`. GENERATED, do not edit.
 *
 * Source: src/types/events.schema.json, exported from app/schemas/events.py.
 * Regenerate with \`npm run gen:types\`. Hand-written helpers live in protocol.ts.
 *
 * Units: money is integer cents (\`*_cents\`, and \`offer_total\` on agreement),
 * percentages are integer basis points (\`*_bp\`, 4500 = 45%), dates are ISO
 * \`YYYY-MM-DD\` strings, timings are float milliseconds.
 */`;

const ts = await compile(schema, "WsProtocol", {
  bannerComment: banner,
  additionalProperties: false,
  unreachableDefinitions: true,
  declareExternallyReferenced: true,
  format: true,
  style: { printWidth: 110, semi: true, singleQuote: false, trailingComma: "all" },
});
// json2ts tags every type with "This interface was referenced by ..."; that is noise here.
const tidy = ts
  .replace(/\n \*\n \* This interface was referenced by `WsProtocol`'s JSON-Schema\n \* via the `definition` "\w+"\./g, "")
  .replace(/\/\*\*\n \* This interface was referenced by `WsProtocol`'s JSON-Schema\n \* via the `definition` "\w+"\.\n \*\/\n/g, "");
await writeFile(OUT, tidy);
console.log(`wrote ${OUT}`);
