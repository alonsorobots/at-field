import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Vite config tuned for Tauri:
// - Fixed port 5174 (5173 is VideoBricks; sibling repo on the same dev box).
// - clearScreen disabled so Rust compile errors stay readable in the terminal.
// - HMR uses the same port; Tauri opens this URL during `tauri dev`.
//
// https://tauri.app/start/frontend/vite/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  clearScreen: false,
  server: {
    port: 5174,
    strictPort: true,
    host: "127.0.0.1",
    hmr: {
      protocol: "ws",
      host: "127.0.0.1",
      port: 5174,
    },
  },
  build: {
    target: "esnext",
    minify: "esbuild",
    sourcemap: false,
  },
});
