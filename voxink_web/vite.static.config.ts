import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  root: 'static',
  base: '/ui/',
  server: { port: 3300, host: '127.0.0.1', strictPort: true },
  preview: { port: 3300, host: '127.0.0.1', strictPort: true },
  publicDir: '../public',
  plugins: [react()],
  build: {
    outDir: '../static-dist',
    emptyOutDir: true,
  },
});
