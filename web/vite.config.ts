/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// FastAPI runs on :8000 in dev (uvicorn app.main:app --reload). Everything the
// UI talks to is proxied so the browser sees one origin, like production.
const API = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    // Recharts is ~545 kB minified (~160 kB gzip) on its own; it is a separate cached chunk.
    chunkSizeWarningLimit: 600,
    rolldownOptions: {
      output: {
        // Charts change far less often than app code: a separate cacheable chunk.
        codeSplitting: {
          groups: [
            { name: "charts", test: /node_modules[\\/](recharts|d3-|victory-vendor|@reduxjs|redux|immer|reselect)/ },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      "/ws": { target: API, ws: true, changeOrigin: true },
      "/scenarios": { target: API, changeOrigin: true },
      "/calls": { target: API, changeOrigin: true },
      "/metrics": { target: API, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
