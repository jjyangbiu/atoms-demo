import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    // 小内存机器（2C4G）构建友好：跳过产物体积报告，避免 gzip 压缩统计的额外内存峰值。
    reportCompressedSize: false,
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        manualChunks: {
          // monaco-editor 体积巨大且模块碎片化，单独分包以降低构建期内存峰值。
          monaco: ['monaco-editor'],
        },
      },
    },
  },
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
