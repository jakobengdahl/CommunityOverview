import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    proxy: {
      // Proxy to backend chat endpoint
      '/chat': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      // Proxy to backend upload endpoint
      '/upload': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      }
    }
  }
})
