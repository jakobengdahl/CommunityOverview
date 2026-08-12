import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// WebXR requires a secure context. The simplest device workflow is USB +
// `adb reverse tcp:5173 tcp:5173`, then open http://localhost:5173 in the Quest
// Browser — localhost counts as secure, so no certificates are needed. `host:
// true` also exposes the dev server on the LAN for an HTTPS-tunnel workflow.
// See README.md for both paths.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
  },
  test: {
    environment: 'node',
  },
});
