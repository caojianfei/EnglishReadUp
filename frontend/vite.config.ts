import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端固定 8010（docs/DESIGN.md §5.2）。
// 开发时把 /api 与 /media 反代过去，前端因此是同源 —— 免 CORS，
// 也让 <img src="/media/..."> 的路径在开发与生产完全一致。
const BACKEND = 'http://127.0.0.1:8010'

// 开发（server）与生产预览（preview）走同一套代理 —— preview 不会继承
// server.proxy，各写一份。少了这块，`npm run build && npm run preview`
// 打开页面时所有 /api 都会 404。
const proxy = {
  '/api': { target: BACKEND, changeOrigin: true },
  '/media': { target: BACKEND, changeOrigin: true },
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 显式绑 IPv4：默认的 'localhost' 在 Windows 上会落到 ::1，
    // 而验收脚本与后端代理都按 127.0.0.1 访问，两边对不上。
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy,
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy,
  },
})
