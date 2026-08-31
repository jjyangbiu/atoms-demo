import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      // 已发布应用的公开链接由后端匿名托管（工单 0006）。
      // 注意必须带尾部斜杠：Vite 代理按前缀匹配，'/p' 会误吞 /projects 等路由路径。
      '/p/': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
