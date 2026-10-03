import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      // Dev mode: the browser talks to vite, vite forwards to FastAPI,
      // so no CORS ever enters the picture. Production has none either:
      // the backend serves dist/ itself.
      '/api': 'http://127.0.0.1:8788',
    },
  },
  build: { outDir: 'dist' },
})
