import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  root: 'static',
  base: '/ui/',
  publicDir: '../public',
  plugins: [react()],
  build: {
    outDir: '../static-dist',
    emptyOutDir: true,
  },
});
