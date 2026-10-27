import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { readFileSync } from 'fs';

const pkg = JSON.parse(readFileSync(path.resolve(__dirname, 'package.json'), 'utf-8'));
// Short commit SHA of the build; injected by the image build, absent in local dev.
const buildSha = (process.env.BUILD_SHA || process.env.GITHUB_SHA || '').slice(0, 7);

export default defineConfig({
  plugins: [react()],
  base: './',
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version || ''),
    __APP_BUILD_SHA__: JSON.stringify(buildSha),
  },
  build: {
    outDir: 'dist',
    emptyDirOnBuild: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ui': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/execute_tool': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/export_graph': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/mcp': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
