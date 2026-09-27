import babel from "@rolldown/plugin-babel";
import tailwindcss from "@tailwindcss/vite";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

/**
 * The dashboard talks to the API on its own origin: in production the edge proxy serves both, in
 * development Vite forwards `/v1` (REST and the `/v1/live` WebSocket) to `VITE_API_URL`.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "VITE_");
  const api = new URL(env.VITE_API_URL || "http://localhost:8080");

  return {
    plugins: [react(), babel({ presets: [reactCompilerPreset()] }), tailwindcss()],
    resolve: { tsconfigPaths: true },
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/v1": {
          target: api.origin,
          changeOrigin: true,
          ws: true,
          // The API only accepts cookie-authenticated sockets from allowed origins. During
          // development the browser's origin is this dev server, which acts as the edge, so the
          // upgrade is presented with the API's own origin.
          configure(proxy) {
            proxy.on("proxyReqWs", (proxyReq) => proxyReq.setHeader("origin", api.origin));
          },
        },
      },
    },
    build: {
      target: "es2022",
      assetsInlineLimit: 0,
      chunkSizeWarningLimit: 1200,
      rolldownOptions: {
        output: {
          codeSplitting: {
            groups: [{ name: "react", test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ }],
          },
        },
      },
    },
    test: {
      environment: "node",
      include: ["src/**/*.test.{ts,tsx}"],
      restoreMocks: true,
    },
  };
});
