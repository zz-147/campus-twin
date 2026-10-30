import { defineConfig } from 'vite';

// 前端开发/构建配置。
// - dev 默认监听 127.0.0.1:5173（端口被占用时自动切换，可用 --port 指定）
// - 后端地址不写死在这里：运行时通过 VITE_API_BASE 环境变量覆盖（默认 http://127.0.0.1:8000）
export default defineConfig({
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
  build: {
    target: 'es2020',
    outDir: 'dist',
    assetsDir: 'assets',
    chunkSizeWarningLimit: 1500,
  },
});
