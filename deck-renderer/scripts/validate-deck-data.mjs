// Validate a deck-data.json against schemas/deck-data.schema.json.
//
// A tiny, dependency-free validator covering the subset of JSON Schema the deck
// contract uses: object/properties/required/additionalProperties, array/items/
// minItems/maxItems, string/minLength/maxLength, enum, and type. The schema file
// stays the single source of truth — this just makes it executable so Agent 3 can
// gate the build on it.
//
// Usage:  node scripts/validate-deck-data.mjs [path/to/deck-data.json]
// Default data path: ./deck-data.json (the renderer's fill file).
// Exits 0 if valid, 1 with a list of field-path errors otherwise.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const schemaPath = path.resolve(here, "../../schemas/deck-data.schema.json");
const dataPath = path.resolve(process.argv[2] || path.resolve(here, "../deck-data.json"));

function load(p, label) {
  try {
    return JSON.parse(readFileSync(p, "utf8"));
  } catch (e) {
    console.error(`✗ Could not read/parse ${label} at ${p}\n  ${e.message}`);
    process.exit(1);
  }
}

const schema = load(schemaPath, "schema");
const data = load(dataPath, "data");
const errors = [];

function check(node, sch, loc) {
  if (sch.type === "object") {
    if (node === null || typeof node !== "object" || Array.isArray(node)) {
      errors.push(`${loc}: expected object`);
      return;
    }
    for (const req of sch.required || []) {
      if (!(req in node)) errors.push(`${loc}.${req}: required but missing`);
    }
    const props = sch.properties || {};
    if (sch.additionalProperties === false) {
      for (const key of Object.keys(node)) {
        if (!(key in props)) errors.push(`${loc}.${key}: unexpected property (not in schema)`);
      }
    }
    for (const [key, subschema] of Object.entries(props)) {
      if (key in node) check(node[key], subschema, `${loc}.${key}`);
    }
    return;
  }

  if (sch.type === "array") {
    if (!Array.isArray(node)) {
      errors.push(`${loc}: expected array`);
      return;
    }
    if (sch.minItems != null && node.length < sch.minItems)
      errors.push(`${loc}: needs at least ${sch.minItems} items (has ${node.length})`);
    if (sch.maxItems != null && node.length > sch.maxItems)
      errors.push(`${loc}: allows at most ${sch.maxItems} items (has ${node.length})`);
    if (sch.items) node.forEach((item, i) => check(item, sch.items, `${loc}[${i}]`));
    return;
  }

  if (sch.type === "string") {
    if (typeof node !== "string") {
      errors.push(`${loc}: expected string`);
      return;
    }
    if (sch.enum && !sch.enum.includes(node))
      errors.push(`${loc}: "${node}" not one of ${JSON.stringify(sch.enum)}`);
    if (sch.minLength != null && node.length < sch.minLength)
      errors.push(`${loc}: too short (min ${sch.minLength})`);
    if (sch.maxLength != null && node.length > sch.maxLength)
      errors.push(`${loc}: too long — ${node.length}/${sch.maxLength} chars. Shorten: "${node.slice(0, 40)}…"`);
    return;
  }
}

check(data, schema, "deck");

if (errors.length) {
  console.error(`✗ deck-data invalid (${errors.length} issue${errors.length > 1 ? "s" : ""}):`);
  for (const e of errors) console.error(`  • ${e}`);
  process.exit(1);
}
console.log(`✓ deck-data is valid (${path.relative(process.cwd(), dataPath)}).`);
