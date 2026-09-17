import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发代理目标：默认指向裸机/桌面形态的默认端口 8566。
// 若后端跑在别处（例如 Docker 映射的 8080），用 VITE_API_TARGET 覆盖：
//   VITE_API_TARGET=http://localhost:8080 npm run dev
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
