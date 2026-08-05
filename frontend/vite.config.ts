/// <reference types="vitest/config" />
import path from "node:path";
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const rootDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: rootDir,
  base: "/static/react/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: path.resolve(rootDir, "../task4_consistency/web/static/react"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
