import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: 'localhost',
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': 'https://dealsync-shopping-app.onrender.com',
      '/auth': 'https://dealsync-shopping-app.onrender.com',
      '/health': 'https://dealsync-shopping-app.onrender.com',
    },
  },
})
