import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// No CDN at runtime (CLIENT_UI_DESIGN.md §5): dependencies are bundled/vendored by Vite, so the
// client renders in the closed environments this tool is for. `base: "./"` keeps asset URLs
// relative, so the built bundle works both served by the Python server and loaded from a file://
// by the Electron shell.
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    // Dev-only convenience: proxy the API to the Python server so the dev client and the API
    // share an origin. Production serves the built client from that same server, so there is no
    // proxy and no CORS to configure.
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
  },
});
