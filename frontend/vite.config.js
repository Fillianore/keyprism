import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  build: {
    chunkSizeWarningLimit: 5000,
  },
  server: {
    port: 5173,
  },
});
