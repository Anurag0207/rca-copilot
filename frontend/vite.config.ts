/**
 * Vite configuration for the NL-to-Data Insight Platform frontend.
 *
 * Dev server proxies /api/* to the FastAPI backend so that CORS is not
 * an issue during local development. In production, the reverse proxy
 * (nginx) handles this routing.
 */

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend runs under `uvicorn --reload`, which kills and restarts the Python
// process on every file change. If the proxy reuses a pooled keep-alive
// socket to the old (now-dead) process, the request hangs on a socket that
// will never respond and Vite eventually answers the browser with a bare
// 504 — before the new backend process ever sees the request (no backend
// log line is emitted). `agent: false` disables connection pooling so every
// proxied request opens a fresh socket instead of risking a stale one.
const backendProxy = { target: "http://localhost:8000", changeOrigin: true, agent: false as const };

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Forward all /auth, /chat, /output requests to the FastAPI backend
      "/auth": backendProxy,
      "/chat": backendProxy,
      "/output": backendProxy,
      "/health": backendProxy,
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
