import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const RESERVED_PORTS = new Set([43_187, 43_188, 43_189, 43_190, 43_191, 43_192]);

function readPort(name: string, fallback: number): number {
  const port = Number(process.env[name] ?? fallback);
  if (!Number.isInteger(port) || port < 1024 || port > 65_535) {
    throw new Error(`${name} is invalid`);
  }
  if (RESERVED_PORTS.has(port)) {
    throw new Error(`${name}=${port} is reserved by local runtime`);
  }
  return port;
}

const webPort = readPort("QF_WEB_PORT", 27_871);
const apiPort = readPort("QF_API_PORT", 27_872);

export default defineConfig({
  plugins: [react()],
  server: {
    host: process.env.QF_WEB_HOST ?? "127.0.0.1",
    port: webPort,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: readPort("QF_PREVIEW_PORT", 27_875),
    strictPort: true,
  },
});
