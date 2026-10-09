import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发代理目标：默认指向本机后端 8566（裸机 / 桌面 / Docker 对外端口已统一为 8566）。
// 若后端跑在别处（其它机器或其它端口），用 VITE_API_TARGET 覆盖：
//   VITE_API_TARGET=http://192.168.1.10:8566 npm run dev
const apiTarget = process.env.VITE_API_TARGET || "http://localhost:8566";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    port: 5173,
    proxy: {
      "/api": apiTarget,
      "/health": apiTarget,
    },
  },
});
