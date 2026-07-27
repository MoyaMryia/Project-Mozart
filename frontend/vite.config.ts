import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    host: true,
    proxy: {
      '/api': 'http://127.0.0.1:18080',
      '/health': 'http://127.0.0.1:18080',
      '/status': 'http://127.0.0.1:18080',
      '/models': 'http://127.0.0.1:18080'
    }
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
