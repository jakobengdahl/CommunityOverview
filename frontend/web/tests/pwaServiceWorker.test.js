import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { describe, expect, it, vi } from 'vitest';

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

describe('PWA service worker', () => {
  it('registers the app-shell worker only when service workers are available', async () => {
    const { registerAppServiceWorker } = await import('../src/pwa/registerServiceWorker');
    const register = vi.fn().mockResolvedValue({});
    const serviceWorker = { register };

    registerAppServiceWorker({
      navigator: { serviceWorker },
      window: { location: { protocol: 'https:' } },
    });

    expect(register).toHaveBeenCalledWith('/service-worker.js', { scope: '/' });
  });

  it('does not try to register over non-local HTTP', async () => {
    const { registerAppServiceWorker } = await import('../src/pwa/registerServiceWorker');
    const register = vi.fn();

    registerAppServiceWorker({
      navigator: { serviceWorker: { register } },
      window: {
        location: {
          protocol: 'http:',
          hostname: 'example.test',
        },
      },
    });

    expect(register).not.toHaveBeenCalled();
  });

  it('caches only static app-shell assets and bypasses API/session/auth requests', async () => {
    const worker = await readFile(path.join(webRoot, 'public', 'service-worker.js'), 'utf8');

    expect(worker).toContain("'/");
    expect(worker).toContain("'/manifest.webmanifest'");
    expect(worker).toContain("'/icon-192.png'");
    expect(worker).toContain('BYPASS_PATH_PREFIXES');
    expect(worker).toContain("'/api/'");
    expect(worker).toContain("'/sessions'");
    expect(worker).toContain("'/session'");
    expect(worker).toContain("'/auth'");
    expect(worker).toContain("'/login'");
    expect(worker).toContain("'/logout'");
    expect(worker).toContain("'/billing'");
    expect(worker).toContain("'/checkout'");
    expect(worker).toContain("'/subscriptions'");
    expect(worker).toContain("'/iap'");
    expect(worker).toContain("event.request.mode !== 'navigate'");
    expect(worker).not.toMatch(/cache\.put\([^)]*event\.request/);
    expect(worker).not.toMatch(/caches\.match\(event\.request\)[\s\S]*shouldBypass/);
  });
});
