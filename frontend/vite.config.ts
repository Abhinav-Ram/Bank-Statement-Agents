import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In docker the API is reachable as http://api:8080; natively, http://localhost:8080.
const target = process.env.API_PROXY_TARGET ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": { target, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
});
