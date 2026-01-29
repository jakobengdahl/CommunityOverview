import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyDirOnBuild: true,
    // Build as a library for embedding
    lib: {
      entry: path.resolve(__dirname, 'src/main.jsx'),
      name: 'CommunityGraphWidget',
      fileName: 'widget',
      formats: ['iife'],
    },
    rollupOptions: {
      // Externalize React if the host page provides it
      external: [],
      output: {
        globals: {},
      },
    },
  },
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
});
