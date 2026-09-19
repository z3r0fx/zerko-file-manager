import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:9600',
        changeOrigin: true,
      },
      '/static': {
        target: 'http://localhost:9600',
        changeOrigin: true,
      },
      '/thumbnails': {
        target: 'http://localhost:9600',
        changeOrigin: true,
      }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  }
})
