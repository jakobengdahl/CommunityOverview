import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for e2e tests
 */
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
      // The mobile specs assert touch-only behaviour (long-press context menus,
      // coarse-pointer layout); they are meaningless on a desktop mouse pointer.
      testIgnore: /mobile-.*\.spec\.js/,
    },
    // Phone viewports: emulated touch plus a coarse pointer, which is what the
    // app's own `(pointer: coarse)` detection keys off. Two devices because the
    // narrow (390px) and wider (412px) viewports catch different layout breaks.
    //
    // Both run on Chromium. The iPhone descriptor defaults to WebKit, but the
    // long-press assertion drives real touch events over CDP, which only
    // Chromium exposes — and pinning one engine keeps CI to a single browser
    // download. These projects check responsive layout and touch behaviour,
    // not engine-specific rendering.
    {
      name: 'mobile-iphone',
      use: { ...devices['iPhone 13'], browserName: 'chromium' },
      testMatch: /mobile-.*\.spec\.js/,
    },
    {
      name: 'mobile-pixel',
      use: { ...devices['Pixel 7'], browserName: 'chromium' },
      testMatch: /mobile-.*\.spec\.js/,
    },
  ],

  // Start the backend and frontend servers before tests
  webServer: [
    {
      command:
        'cd ../../ && python -m uvicorn backend.api_host.server:get_app --factory --host 127.0.0.1 --port 8000',
      url: 'http://localhost:8000/health',
      reuseExistingServer: !process.env.CI,
      timeout: 120 * 1000,
      // Applied only when Playwright starts this server. Locally,
      // reuseExistingServer attaches to whatever is already on port 8000 and
      // this block is ignored — so a dev server started by hand keeps its own
      // key and writes e2e nodes into its own graph file.
      env: {
        // The chat panel only renders when the backend reports an LLM as
        // available. No request ever reaches a provider — the specs stop at the
        // composer — so a placeholder value is enough to exercise the panel.
        ANTHROPIC_API_KEY: 'test-key-for-e2e',
        GRAPH_FILE: 'data/e2e/graph.json',
      },
    },
    {
      command: 'npm run dev',
      url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 120 * 1000,
    },
  ],
});
