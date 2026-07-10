// Generate a static PDF leave-behind of the Bites AI SDR Playbook deck from the
// single-file HTML build.
//
// Drives Bites-AI-SDR-Playbook.html with headless Chromium: renders each slide at
// the deck's native 1280x720 canvas, waits for entrance animations to settle AND
// the presenter chrome to auto-hide, screenshots it, then assembles one landscape
// page per slide into a single PDF. Slide count is read from the deck's own "n / N"
// counter.
//
// Run after `npm run build:deck-bites`.
import { chromium } from "@playwright/test";
import { PDFDocument } from "pdf-lib";
import { writeFile, access } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outDir = path.join(root, "export-deck-bites");
const htmlPath = path.join(outDir, "Bites-AI-SDR-Playbook.html");
const pdfPath = path.join(outDir, "Bites-AI-SDR-Playbook.pdf");

const STAGE_W = 1280;
const STAGE_H = 720;
const SETTLE_MS = 3000;

await access(htmlPath).catch(() => {
  console.error(`✗ ${path.relative(root, htmlPath)} not found. Run \`npm run build:deck-bites\` first.`);
  process.exit(1);
});

// DECK_CHROMIUM_PATH overrides the browser binary when the environment ships a
// Chromium that doesn't match this @playwright/test's pinned revision.
const browser = await chromium.launch(
  process.env.DECK_CHROMIUM_PATH ? { executablePath: process.env.DECK_CHROMIUM_PATH } : {},
);
const page = await browser.newPage({
  viewport: { width: STAGE_W, height: STAGE_H },
  deviceScaleFactor: 2,
});

await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
await page.waitForSelector("#root *", { timeout: 30_000 });

const total = await page.evaluate(() => {
  const m = document.body.innerText.match(/\b(\d+)\s*\/\s*(\d+)\b/);
  return m ? parseInt(m[2], 10) : null;
});
if (!total) {
  console.error("✗ Could not determine slide count from the deck.");
  await browser.close();
  process.exit(1);
}
console.log(`Capturing ${total} slides…`);

await page.keyboard.press("Home");

const shots = [];
for (let i = 0; i < total; i++) {
  await page.waitForTimeout(SETTLE_MS);
  shots.push(await page.screenshot({ type: "png" }));
  process.stdout.write(`  ✓ slide ${i + 1}/${total}\n`);
  if (i < total - 1) await page.keyboard.press("ArrowRight");
}

await browser.close();

const pdf = await PDFDocument.create();
for (const png of shots) {
  const img = await pdf.embedPng(png);
  const p = pdf.addPage([STAGE_W, STAGE_H]);
  p.drawImage(img, { x: 0, y: 0, width: STAGE_W, height: STAGE_H });
}
await writeFile(pdfPath, await pdf.save());

console.log(`\n✅ PDF: export-deck-bites/Bites-AI-SDR-Playbook.pdf (${total} pages)`);
