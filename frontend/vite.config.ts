import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Natively the API runs on http://localhost:8080; in docker the web container reaches it via
// host.docker.internal (set API_PROXY_TARGET).
const target = process.env.API_PROXY_TARGET ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": { target, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
});
