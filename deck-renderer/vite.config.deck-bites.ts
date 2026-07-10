import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import { viteSingleFile } from "vite-plugin-singlefile";
import path from "path";

/**
 * Dedicated build for the standalone, shareable Bites AI SDR Playbook deck.
 *
 * Produces ONE self-contained HTML file (all JS, CSS, fonts, and logos inlined)
 * that can be downloaded and opened directly in any browser — no server, no
 * network. Entry is bites-deck-export.html -> src/bites-deck-main.tsx.
 *
 * Fully independent of vite.config.ts and the other deck configs.
 * Run via `npm run build:deck-bites`.
 */
export default defineConfig({
  base: "./",
  publicDir: false,
  plugins: [react(), viteSingleFile()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime"],
  },
  build: {
    outDir: "export-deck-bites",
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "bites-deck-export.html"),
    },
  },
});
