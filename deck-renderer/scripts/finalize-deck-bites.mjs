// Rename the single-file Bites build to a friendly, shareable name.
// Run after `vite build --config vite.config.deck-bites.ts`.
import { rename, stat, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outDir = path.join(root, "export-deck-bites");
const from = path.join(outDir, "bites-deck-export.html");
const to = path.join(outDir, "Bites-AI-SDR-Playbook.html");

await rename(from, to);

const { size } = await stat(to);
const mb = (size / 1024 / 1024).toFixed(1);
const siblings = (await readdir(outDir)).filter((f) => f !== "Bites-AI-SDR-Playbook.html");

console.log(`\n✅ Single-file deck: export-deck-bites/Bites-AI-SDR-Playbook.html (${mb} MB)`);
if (siblings.length) {
  console.log(`   (note: ${siblings.length} other file(s) in the dir — not needed; the .html is standalone)`);
}
