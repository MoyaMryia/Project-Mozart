import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    proxy: {
      '/api': 'http://127.0.0.1:18080',
      '/health': 'http://127.0.0.1:18080',
      '/status': 'http://127.0.0.1:18080',
      '/models': 'http://127.0.0.1:18080'
    },
    // SSE 不缓冲
  },
  preview: {
    host: true,
    proxy: {
      '/api': 'http://127.0.0.1:18080',
      '/health': 'http://127.0.0.1:18080',
      '/status': 'http://127.0.0.1:18080',
      '/models': 'http://127.0.0.1:18080'
    }
  }
});
